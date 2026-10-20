# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库概述

这是一个 HLS 视频下载与格式转换工具：

- `src/m3u8_cli/`：唯一维护的实现，打包为 `m3u8_cli` 包，提供 `m3u8-cli` 命令和 `python -m m3u8_cli` 两种入口。CLI 通过 argparse 子命令提供 `download`（下载 HLS）、`convert`（转换本地视频）与 `serve`（REST 下载服务）。下载流程并发下载分片、本地缓存/断点续传、AES-128 解密、本地合并后调用 FFmpeg 无损封装，所有下载产物写入以输出文件名派生的任务子目录。
- `chrome-extension/`：配套 Chrome 扩展（Manifest V3、原生 JS、加载时无需构建）。通过 `webRequest` 观察与 DOM 扫描识别 m3u8，可复制链接/CLI 命令，并把候选连同该请求的 `Referer`/`Cookie`/`User-Agent` 提交到 `serve` API；后台用 `chrome.alarms` 轮询任务状态。`scripts/build_extension.py` 负责把它打成可分发的 zip。
- `reference/hls_downloader.py`：早期把 m3u8 直接交给 FFmpeg 的轻量实现，仅作参考保留，不再维护或修改。

运行环境依赖 `ffmpeg`（需在 PATH 中）和 `uv`。Python 版本固定为 3.12（见 `.python-version`）。

## 常用命令

```bash
# 安装依赖并更新入口点
uv sync

# 主下载器（并发 + 缓存）
# -o output.mp4 会生成 output.mp4 与 output.mp4.meta.json（分片缓存在系统临时目录）
uv run m3u8-cli download "<m3u8_url>" -o output.mp4 -j 8
uv run python -m m3u8_cli download "<m3u8_url>" -o output.mp4 -j 8

# 需要鉴权时
uv run m3u8-cli download "<url>" --referer "<referer>" --cookie "<cookie>"

# 转换视频（默认 HLS）
uv run m3u8-cli convert movie.mp4 -o out
uv run m3u8-cli convert movie.mp4 -f mkv -c hevc -o out

# 保留 HLS（不合并）
uv run m3u8-cli download "<m3u8_url>" -f hls -o movie

# 启动 REST API
uv run m3u8-cli serve --host 0.0.0.0 --port 8787 --output-dir ./downloads

# 单元测试
uv run python -m unittest discover -s tests -v

# Chrome 扩展：语法检查与纯函数测试
for f in chrome-extension/{lib,background,content,popup}.js; do node --check "$f"; done
node --test chrome-extension/lib.test.js

# 打包 Python 包（sdist 同时包含 chrome-extension/ 与打包脚本）
uv build

# 打包 Chrome 扩展 zip（输出 dist/m3u8-cli-chrome-extension-<版本>.zip）
uv run python scripts/build_extension.py

# 一次构建全部产物
uv build && uv run python scripts/build_extension.py
```

仓库目前没有 lint 或格式化配置，也没有 CI；单元测试使用标准库 unittest。

## 架构

### 模块划分

- `cli.py`：argparse 子命令（`download` / `convert` / `serve`）、顶层 `-v/--version`（读取包内 `__version__`）、参数校验、流程编排、`state.json` 指纹处理；`main()` 根据 `args.command` 分发到 `run_download()` / `run_convert()` / `run_serve()`，并统一捕获 `HLSException` / `OSError` / `requests.RequestException` / `subprocess.CalledProcessError`。
- `download.py`：可复用的 `DownloadOptions` 与 `download_hls()`，mp4/hls 两条输出路径、进度回调与协作式取消；成功后用 ffprobe 生成元数据文件（`<产物>.meta.json` / `<任务目录>/meta.json`：顶层为 ffprobe 的 `format`/`streams`（去掉 `filename`），另加 `source` 来源信息，不含 `output` 与请求头；ffprobe 缺失或失败时打印警告并只写 `source`），mp4 合并/hls 导出成功后删除系统临时目录中的分片缓存（失败时保留以便续传）。
- `hls_export.py`：生成扁平本地 HLS 目录（playlist、分片、fMP4 init、密钥），`--overwrite` 只清理生成条目。
- `serve.py`：TaskManager（FIFO、并发上限、取消、JSON 持久化）与标准库 HTTP API（`/api/tasks`、`/api/version`）；回环来源（`is_loopback_address()`，含 IPv4-mapped IPv6）默认跳过 token 校验（`make_handler(..., allow_local=False)` 仅供测试关闭）；任务请求的 `insecure`（布尔，默认 false）传给 `DownloadOptions.verify = not insecure`。
- `convert.py`：格式白名单、编码映射、输出路径派生、覆盖检查与 FFmpeg 调用。
- `playlist.py`：播放列表获取与解析。递归解析 master playlist 并选择 `BANDWIDTH` 最高的变体（最多 5 层），解析 `EXT-X-KEY` / `EXT-X-MAP`，返回分片列表和播放列表原文的 SHA-256 指纹。
- `downloader.py`：`Downloader` 负责单个 URL 的获取、重试、密钥缓存和 AES-128 解密。每个线程持有自己的 `requests.Session`（`threading.local`），`verify` 参数直接映射到 `Session.verify`（`--insecure`/API `insecure` 时为 False）；密钥按 URL 全局缓存并加锁。已存在且非空的缓存文件会被直接复用，实现断点续传；写入时先写 `.part` 再原子替换。
- `paths.py`：`temp_cache_dir()` 把分片缓存放到 `tempfile.gettempdir()/m3u8-cli-<uid>/<最终路径哈希>`（mp4 与 hls 共用，同一 `-o` 可续传）；`resolve_mp4_paths()` 保持 mp4 最终路径不变；`fit_output_name()` 按平台截短文件名（Windows 按 `MAX_PATH` 预算，检查最终文件/同目录 `.partial` 与系统临时缓存；POSIX 按 255 字节组件上限）。
- `merge.py`：`combine_segments()` 按播放列表顺序拼接本地分片（fMP4 先写初始化片段）；`mux()` 对本地 `combined.*` 运行一次 FFmpeg（`-map 0 -c copy -movflags +faststart`），先输出到最终文件同目录的 `<stem>.partial<后缀>`，成功后原子替换（缓存可能位于另一个文件系统，不能把临时文件放在缓存目录再 rename）。
- `errors.py`：共享的 `HLSException`。

### 处理流程

1. **播放列表解析** — `playlist.load_playlist()` 选择最高带宽变体并生成分片元数据。
2. **并发下载** — `downloader.Downloader` 配合 `ThreadPoolExecutor` 下载分片和初始化片段。
3. **解密** — 仅支持 `METHOD=AES-128` 且 `KEYFORMAT=identity`。未提供 `IV` 标签时，按 HLS 规范用分片媒体序列号（16 字节大端）作为 IV。
4. **合并与封装** — `merge.combine_segments()` 在本地拼接，`merge.mux()` 调用 FFmpeg 无损封装。

合并顺序以播放列表中的分片索引为准，不能用 `as_completed()` 的完成顺序。`-f hls` 时跳过合并与 FFmpeg，分片按原始字节导出，AES-128 密钥本地化。

### 服务流程

1. `POST /api/tasks` 校验 token 与参数，任务进入 FIFO 队列。
2. `TaskManager` 按 `--max-tasks` 启动工作线程，复用 `download_hls()`。
3. `GET /api/tasks` / `GET /api/tasks/{id}` 查询状态与进度；`DELETE` 触发协作式取消。
4. 状态写入 `<output-dir>/.m3u8-serve-state.json`，请求头只保留在内存。

### Chrome 扩展

`chrome-extension/` 是无构建的 MV3 扩展（Chrome 116+），文件职责：

- `manifest.json`：权限仅 `webRequest`、`storage`、`alarms` 与 `<all_urls>` host permission；content script 注入所有 http/https 页面并开启 `all_frames`。
- `lib.js`：三端共享纯函数（URL/MIME 判断、页面标题清洗、输出名清洗与派生、shell 转义、CLI 命令），经典脚本 + UMD 导出；`lib.test.js` 用 Node 内置 test runner 测试。
- `background.js`：顶层注册 `webRequest` 观察器。`onSendHeaders`（`extraHeaders`）按 `requestId` 缓存 `Cookie/Referer/User-Agent`，`onHeadersReceived` 在请求为 GET/HEAD、状态 2xx 且 URL 以 `.m3u8/.m3u` 结尾或 Content-Type 为 HLS MIME 时创建候选，并通过 `chrome.tabs.get()` 补标签页标题；`onCompleted/onErrorOccurred` 清理缓存。不拦截请求，也不 hook 页面 fetch/XHR。
- `content.js`：扫描 `video/audio/source`、m3u8 链接与内联脚本文本，MutationObserver 去抖后只上报新增 URL；同时按 `og:title` → `twitter:title` → 视频 JSON-LD → `document.title` 采集标题并随消息上报。
- `popup.*`：候选列表、复制/复制 CLI/下载、任务进度与设置。popup 不直接调用 serve API。

存储与消息协议（改动时同步更新 `chrome-extension/README.md` 与根 README）：

- `storage.session["tab:<tabId>"]`：`{pageUrl, pageTitle, items:[{..., pageTitle}]}` 候选列表（每 tab 最多 30 条），输出名优先用标题、缺失时回退 URL 派生名；`storage.session["hdr:<candidateId>"]`：敏感请求头，仅 background 读取。
- `storage.local["settings"]`：服务地址、token（本机 serve 可留空）、格式、`naming`（`title` 页面标题 / `hash` URL 短 hash）、overwrite、sendCookie、copyCookie。
- `storage.local["tasks"]`：任务摘要（最多 20 条，优先保留活动任务），不含请求头。
- 消息：`dom:candidates`（content→background）、`download:submit`、`task:cancel`、`tasks:refresh`、`settings:test`、`server:version`、`cli:build`、`tab:clear`、`dom:rescan`（popup→background）。`server:version` 调用 `GET /api/version`，设置页据此显示「插件 vX · 后端 vY」。
- 下载必须由用户点击触发；仅透传白名单请求头，Cookie 只在 `storage.session`，不渲染、不落盘。非 loopback 服务地址且开启 Cookie 透传时 UI 显示警告。
- `apiFetch()` 统一加 Bearer token 与 8 秒超时；任务轮询由 popup 每 2 秒触发，popup 关闭后由 `chrome.alarms` 以 30 秒（旧版 1 分钟）周期继续。

### 转换流程

`convert.py` 先派生输出目录并检查覆盖，然后执行一次 FFmpeg 调用：HLS 写入 `playlist.m3u8` 与 `ts/%06d.ts`（播放列表通过 `-hls_base_url ts/` 引用分片）；`--encrypt` 时额外生成 16 字节 `key.bin`，并通过临时 `.key_info.txt` 交给 `-hls_key_info_file` 做 AES-128 加密，转换结束删除临时文件（转换失败时同时删除新写入的 `key.bin`）；单文件先写 `.partial.*`，成功后原子替换。`--overwrite` 的 HLS 清理发生在转换前，只删除旧 `playlist.m3u8` 和 `ts/` 中 6 位编号的分片，保留其他文件（`--encrypt` 时 `key.bin` 随后被新密钥覆盖；转换失败时新密钥也会被删除），失败可能丢失旧产物；`ts/` 不允许是符号链接，未指定 `--overwrite` 时悬空的目标符号链接也视为已存在，指定时原子替换链接本身而不跟随目标；陈旧的 `.partial.*` 会在调用 FFmpeg 前删除。单文件输出为 `<输出目录>/<输入文件 stem>.<扩展名>`；未指定 `-o` 时输出目录为当前目录下的输入文件 stem。

### 下载输出布局

两种格式的分片缓存都位于 `<系统临时目录>/m3u8-cli-<uid>/<最终路径 SHA-256 前 16 位>/`，成功导出/合并后删除，失败或取消时保留以便续传。临时缓存布局：

```
<cache>/
  state.json        # {"fingerprint": ...}；播放列表变化时清理旧分片
  segment_%06d.bin  # 已解密的媒体分片，按播放列表索引编号
  map_%04d.bin      # fMP4 初始化片段
  combined.ts|mp4   # 合并后的中间文件（仅 mp4 流程使用）
```

**hls（`-f hls`）**：`-o movie` 直接作为输出任务目录，导出成功后写入 `movie/meta.json`，输出目录不保留 cache。

```
movie/
  playlist.m3u8       # 本地可播放的播放列表
  000000.ts           # 导出的分片（fMP4 为 .m4s，另有 init.mp4）
  key.bin             # 启用密钥时
  meta.json           # ffprobe format/streams + source（URL、格式、分片数、指纹、时间）
```

**mp4（默认）**：最终文件就是 `-o` 指定的路径（`movie.mp4`），元数据写入 `<最终文件>.meta.json`（例如 `movie.mp4.meta.json`）。FFmpeg 的临时输出为最终文件同目录的 `<stem>.partial<后缀>`，保证跨文件系统时仍能原子替换。

下载输出与缓存布局是持久化契约，改动时必须同时更新下载、失效清理、合并与扩展 README。

### 参考实现

`reference/hls_downloader.py` 通过单次 FFmpeg 调用完成下载、解密和封装，不做分片缓存，也无断点续传，且未采用任务子目录布局。不要修改此文件。

### 打包

`pyproject.toml` 使用 `uv_build` 后端，自动从 `src/` 发现 `m3u8_cli` 包；`[project.scripts]` 的 `m3u8-cli = "m3u8_cli.cli:main"` 是命令入口，`src/m3u8_cli/__main__.py` 支持 `python -m m3u8_cli`。

`[tool.uv.build-backend] source-include` 把 `chrome-extension/**` 与 `scripts/build_extension.py` 纳入 sdist，wheel 仍只包含 Python 包。

`scripts/build_extension.py` 读取 `manifest.json` 的版本与运行时文件，校验 manifest 引用与 `RUNTIME_FILES` 一致后，以固定时间戳生成确定性的 `dist/m3u8-cli-chrome-extension-<version>.zip`。新增扩展运行时文件时必须同步脚本中的 `RUNTIME_FILES`；`tests/test_build_extension.py` 覆盖产物内容、确定性与非法 manifest/缺失文件场景。

## 已知限制

- 不支持 `EXT-X-BYTERANGE`，也不支持带 BYTERANGE 的初始化片段。
- 只支持单个初始化片段；检测到多个不同 `EXT-X-MAP` 会直接报错。
- 只支持 AES-128（identity key format），不处理 SAMPLE-AES 或任何 DRM。
- 播放列表只抓取一次，不刷新直播/事件播放列表的滑动窗口。
- `convert` 只接受本地文件；不探测源编码；HLS 转换在 `--overwrite` 时会先删除旧产物，没有整体原子性。
- `--encrypt` 仅用于 HLS；密钥与播放列表同目录分发，FFmpeg muxer 使用固定 IV=0，本地播放/探测需 `-allowed_extensions ALL`。AES-128 加密不是 DRM：能访问 `key.bin` 的客户端即可解密。
- serve 不做 SSRF 内网地址屏蔽，依赖 token 与局域网部署。
- 回环地址请求免 token（对同一台机器的所有用户生效）；多用户主机或本机反向代理场景需自行评估，必要时只监听 `127.0.0.1`。由于 CORS 为 `*`，免 token 时任意网页也能调用本机 API。
- serve 不提供下载产物接口，文件留在服务器。
- Windows 下 serve 会按 `MAX_PATH` 把过长的 `output` 文件名截短（保留扩展名），任务响应返回实际使用的名字；CLI 的 `-o` 不做改写，超长路径需用户自行处理，扩展可切换到「URL 短 hash」命名规避。
- mp4 与 hls 的分片缓存都位于系统临时目录：成功后删除，失败/取消保留以便续传；操作系统清理临时目录后需要重新下载。
- 元数据文件记录完整来源 URL（可能含临时 token）与 ffprobe 媒体信息，不含 `output`、请求头/Cookie；ffprobe 不存在时只写 `source` 并警告。
- `--insecure` / API `insecure` / 扩展「跳过 TLS 证书校验」会关闭源站证书与主机名校验，只应在信任的自签名/内网服务上使用。
- serve 重启后任务标记为 `interrupted`，不会自动恢复。
- 请求头不落盘，重启后无法重放任务。
- Chrome 扩展只做网络观察与 DOM 扫描，不 hook 页面 fetch/XHR；缓存命中或没有实际请求的地址可能只由 DOM 扫描发现。
- 扩展候选存于 `storage.session`（浏览器重启清空），每个 tab 最多 30 条；任务历史最多 20 条。
- 扩展任务轮询最短 30 秒（旧版 Chrome 为 1 分钟），popup 打开时才每 2 秒刷新。
- SPA 路由切换不会自动清空候选；扩展重载后已有页面需要刷新才能恢复 content script。
- 扩展不支持 Shadow DOM 内的元素，且无法把 `blob:` 流直接交给 serve。
- 运行产物已在 `.gitignore` 中忽略（`*.mp4`、`*.mkv`、`*.webm`、任意目录下的 `playlist.m3u8`、`**/ts/` 与任务目录顶层 6 位编号的 `.ts`/`.m4s` 分片、`key.bin`、`key_%03d.bin`（编号超过 999 时位数增加）、`.m3u8-serve-state.json`、`.key_info.txt`、`*.part`、`*.partial.*`、`*.hls-cache/`、`**/cache/`）；`init.mp4` 由 `*.mp4` 覆盖。不要用 `git add -f` 把任务目录、转换产物或缓存加入 Git。

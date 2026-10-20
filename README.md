# m3u8-cli

一个基于 Python 的 HLS（m3u8）视频下载与格式转换工具：并发下载分片、支持 AES-128 解密、本地缓存与断点续传，最后通过 FFmpeg 无损封装为 MP4；也可以把本地视频转换为 HLS 或其他常见格式。

## 特性

- **清晰度选择**：自动解析 master playlist，选择 `BANDWIDTH` 最高的变体。
- **并发下载**：多线程下载分片，默认 8 并发，可按需调整。
- **AES-128 解密**：支持标准 HLS AES-128 加密（`KEYFORMAT=identity`）。
- **断点续传**：分片保存在本地任务目录，中断后重新运行会复用已下载分片。
- **任务隔离**：最终视频与中间缓存集中放在以输出文件名命名的子目录中。
- **无损封装**：本地合并分片后一次性调用 FFmpeg，使用 `-c copy` 与 `+faststart` 封装为 MP4。
- **格式转换**：把本地视频转换为 HLS、MP4、MKV 或 WebM，支持 H.264、HEVC 与流复制。

## 环境要求

- Python 3.12+
- [FFmpeg](https://ffmpeg.org/)（需要能在 `PATH` 中找到 `ffmpeg`）
- [uv](https://docs.astral.sh/uv/) 或 pip（任选其一用于安装）

## 安装

### 使用 uv（推荐）

从远程仓库安装为全局命令：

```bash
uv tool install git+<仓库地址>
```

也可以克隆后从本地目录安装：

```bash
git clone <仓库地址>
cd m3u8-cli
uv tool install .
```

安装后 `m3u8-cli` 已在 `PATH` 中，日常直接运行即可，不需要 `uv run`。

若参与开发，则在仓库目录中执行 `uv sync` 把依赖装进项目的 `.venv`，此时需要使用 `uv run` 前缀。

### 使用 pip

从远程仓库安装：

```bash
pip install git+<仓库地址>
```

或者克隆后从本地目录安装：

```bash
git clone <仓库地址>
cd m3u8-cli
pip install .
```

升级时加 `--upgrade`：`pip install --upgrade .`。若系统的 Python 启用了 externally-managed 保护（如 Arch、Debian 的默认配置），请先创建并激活虚拟环境再执行上述命令。

## 使用

```bash
m3u8-cli download "<m3u8_url>"
```

也可以以模块方式运行：

```bash
python -m m3u8_cli download "<m3u8_url>"
```

转换同样支持模块方式：

```bash
python -m m3u8_cli convert movie.mp4 -o out
```

### 常用示例

```bash
# 默认输出到 output/output.mp4
m3u8-cli download "https://example.com/playlist.m3u8"

# 指定输出文件名与并发数
m3u8-cli download "https://example.com/playlist.m3u8" -o movie.mp4 -j 16

# 需要 Referer / Cookie 等请求头时
m3u8-cli download "https://example.com/playlist.m3u8" \
  --referer "https://example.com/" \
  --cookie "session=xxxx"

# 覆盖已存在的最终文件
m3u8-cli download "https://example.com/playlist.m3u8" -o movie.mp4 --overwrite
```

### 转换视频

```bash
# 默认转换为 HLS：当前目录下生成 movie/playlist.m3u8 与 movie/ts/*.ts
m3u8-cli convert movie.mp4

# 指定输出目录、格式与编码
m3u8-cli convert movie.mp4 -f mkv -c hevc -o out

# 转换 HLS 并设置每片 10 秒
m3u8-cli convert movie.mp4 -f hls --segment-time 10

# 覆盖已有产物
m3u8-cli convert movie.mp4 -f mp4 --overwrite

# 加密 HLS 分片（生成 key.bin）
m3u8-cli convert movie.mp4 --encrypt
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `input` | 必填 | 本地视频文件路径 |
| `-f, --format` | `hls` | `hls`、`mp4`、`mkv`、`webm` |
| `-c, --codec` | `h264` | `h264`、`hevc`、`copy` |
| `-o, --output-dir` | 输入文件 stem | 输出目录 |
| `--segment-time` | `6` | 仅 HLS 使用 |
| `--overwrite` | 关闭 | 覆盖已有产物 |
| `--encrypt` | 关闭 | 仅 HLS；生成 `key.bin` 并加密分片 |

HLS 输出固定为 `playlist.m3u8` 加 `ts/000000.ts` 起的分片；WebM 重编码时使用 VP9 + Opus，`-c copy` 则直接复制源编码并由 FFmpeg 判断兼容性。

- `mp4`/`mkv`/`webm` 输出为 `<输出目录>/<输入文件 stem>.<扩展名>`，例如 `m3u8-cli convert movie.mp4 -f mkv -o out` 生成 `out/movie.mkv`；单文件先写入 `.partial.*`，成功后原子替换。
- `-c copy` 时 HLS 分片边界由源关键帧决定，`--segment-time` 只是近似目标。
- `--encrypt` 仅用于 HLS：输出目录会额外生成 16 字节的 `key.bin`，播放列表通过相对 URI 引用；分发时必须与 `playlist.m3u8` 放在一起。
- FFmpeg/ffprobe 默认拒绝 `key.bin` 扩展名，本地播放或探测需加 `-allowed_extensions ALL`；浏览器播放器不受此限制。
- 每次 `--encrypt` 转换都会生成新密钥；未指定 `--overwrite` 时已有 `key.bin` 或 `.key_info.txt` 会阻止转换，指定后会替换旧密钥，需要重新分发 `key.bin`。

### 下载参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `url` | 必填 | m3u8 播放列表地址 |
| `-f, --format` | `mp4` | `mp4`（合并封装）或 `hls`（保留分片，不合并） |
| `-o, --output` | `output.mp4` / `output` | mp4 为最终文件名；hls 为任务目录（格式不同，默认值不同） |
| `-j, --jobs` | `8` | 并发下载线程数 |
| `--retries` | `3` | 单个分片下载失败后的重试次数 |
| `--timeout` | `30` | 网络请求超时（秒） |
| `--referer` | 无 | 自定义 `Referer` 请求头 |
| `--cookie` | 无 | 自定义 `Cookie` 请求头 |
| `--user-agent` | `Mozilla/5.0` | 自定义 `User-Agent` 请求头 |
| `--overwrite` | 关闭 | 覆盖已存在的最终文件 |

### 保留 HLS（不合并）

```bash
# 输出 movie/playlist.m3u8 与 movie/000000.ts 等分片
m3u8-cli download "<m3u8_url>" -f hls -o movie
```

- `-f hls` 不调用 FFmpeg；分片保持服务器返回的原始字节。
- 普通 TS 流输出 `playlist.m3u8` 与 6 位编号的 `.ts` 分片；fMP4 流输出 `init.mp4` 与 `.m4s` 分片。
- 源流为 AES-128 加密时保持密文，同时把密钥下载为 `key.bin`（多个不同密钥时为 `key_000.bin`、`key_001.bin`），播放列表的 `EXT-X-KEY` URI 重写为本地文件。
- 分片、`playlist.m3u8` 与密钥文件位于同一目录；`--overwrite` 只清理本工具生成的条目。

### 启动下载服务

```bash
m3u8-cli serve --host 0.0.0.0 --port 8787 --output-dir ./downloads
```

启动日志会打印监听地址、输出目录与 API token；省略 --token 时会自动生成。

```bash
# 创建任务
curl -X POST http://127.0.0.1:8787/api/tasks \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/playlist.m3u8",
    "format": "hls",
    "output": "movie",
    "headers": {"Referer": "https://example.com/"}
  }'

# 任务列表 / 详情
curl http://127.0.0.1:8787/api/tasks -H "Authorization: Bearer <token>"
curl http://127.0.0.1:8787/api/tasks/<id> -H "Authorization: Bearer <token>"

# 取消任务
curl -X DELETE http://127.0.0.1:8787/api/tasks/<id> \
  -H "Authorization: Bearer <token>"
```

- 全局最多 `--max-tasks` 个任务同时运行，其余 FIFO 排队；每个任务内部由 `jobs` 控制分片并发。
- 请求头通过 `headers` 对象传入，用于播放列表、分片与密钥请求，不会被持久化或返回。
- 状态文件默认 `<output-dir>/.m3u8-serve-state.json`；重启后未完成任务标记为 `interrupted`。
- 任务列表支持 `?state=running`、`?limit=50&offset=0`；`total` 为过滤后的数量。
- `--port 0` 时由系统分配端口，实际端口会打印在启动日志中。

## Chrome 扩展

`chrome-extension/` 提供一个 Manifest V3 扩展，用于识别页面中的 m3u8 链接：

- 监听页面网络请求并结合 DOM 扫描，列出当前标签页的 m3u8 地址；
- 复制链接，或生成带 `Referer`/`Cookie`/`User-Agent` 的 `m3u8-cli download` 命令；
- 一键提交到本地 `m3u8-cli serve` 下载，并在弹窗中查看进度、取消任务。

安装步骤：打开 `chrome://extensions`，开启“开发者模式”，点击“加载已解压的扩展程序”，选择仓库中的 `chrome-extension/` 目录。

建议以固定 token 和本机监听启动服务，然后在扩展设置中填入相同地址与 token：

```bash
m3u8-cli serve --host 127.0.0.1 --port 8787 --token mytoken --output-dir ./downloads
```

权限、Cookie 处理与排障说明见 [`chrome-extension/README.md`](chrome-extension/README.md)。

## 下载输出结构

以 `-o movie.mp4` 为例：

```
movie/
├── movie.mp4              # 最终产物
└── cache/
    ├── state.json         # 播放列表指纹，用于缓存失效判断
    ├── segment_000000.bin # 已解密的媒体分片
    ├── map_0000.bin       # fMP4 初始化片段（仅 fMP4 流）
    ├── combined.ts|mp4    # 本地合并的中间文件
    └── movie.partial.mp4  # FFmpeg 临时输出
```

- 任务目录由输出文件名自动派生；若输出路径已经是 `movie/movie.mp4` 形式，会复用该目录，不会重复嵌套。
- 最终文件生成成功后，缓存仍会保留在任务目录中。

## 缓存与续传

- 使用相同 `-o` 且任务目录仍在时，已存在且非空的分片会被直接复用。
- `cache/state.json` 记录播放列表原文的 SHA-256 指纹；播放列表变化时会清除旧分片。
- 下载时最终文件已存在且未指定 `--overwrite`，程序会直接退出。
- 需要彻底清理时，删除整个任务目录即可。

## 已知限制

- 不支持 `EXT-X-BYTERANGE`，也不支持带 BYTERANGE 的初始化片段。
- 只支持单个初始化片段（`EXT-X-MAP`）。
- 只支持 AES-128（`KEYFORMAT=identity`），不支持 SAMPLE-AES 或任何 DRM。
- 播放列表只抓取一次，不刷新直播或事件播放列表的滑动窗口。
- 不支持多音轨/多字幕轨道的选择性下载。
- `convert` 仅接受本地文件；`-c copy` 不做源编码兼容性预检。
- HLS 转换在 `--overwrite` 时会先删除旧 `playlist.m3u8` 和 `ts/` 中 6 位编号的分片，保留其他文件（`--encrypt` 时 `key.bin` 会被新密钥替换）；转换失败将丢失旧产物，并可能留下部分新分片，新写入的 `key.bin` 也会被删除。
- HLS AES-128 加密不是 DRM；能访问 `key.bin` 的客户端即可解密。
- FFmpeg 的 HLS muxer 会写入固定 `IV=0`，所有分片共用同一密钥与 IV。
- 不支持自定义密钥、密钥 URI 或 IV。
- serve 不做 SSRF 内网地址屏蔽，依赖 token 与局域网部署。
- serve 不提供下载产物接口，文件留在服务器。
- serve 重启后任务标记为 `interrupted`，不会自动恢复。
- 请求头不落盘，重启后无法重放任务。

## 开发

```bash
# 编译检查
uv run python -m compileall -q src

# 类型检查（使用 .venv）
pyright

# 单元测试
uv run python -m unittest discover -s tests -v

# Chrome 扩展：语法检查与单元测试
for f in chrome-extension/{lib,background,content,popup}.js; do node --check "$f"; done
node --test chrome-extension/lib.test.js

# 构建 wheel
uv build
```

项目目前没有 lint 或 CI 配置，Python 单元测试使用标准库 unittest，Chrome 扩展的纯函数测试使用 Node 内置 test runner。

代码结构：

```
src/m3u8_cli/
├── cli.py           # 命令行入口与流程编排
├── convert.py       # 视频格式转换（FFmpeg）
├── playlist.py      # m3u8 解析
├── download.py      # 可复用下载核心（mp4/hls、进度、取消）
├── hls_export.py    # 本地 HLS 导出
├── serve.py         # REST API 与任务管理
├── downloader.py    # 单分片下载、重试与 AES-128 解密
├── merge.py         # 分片合并与 FFmpeg 封装
├── paths.py         # 任务目录派生
├── errors.py        # 共享异常
└── __main__.py      # python -m m3u8_cli
```

`chrome-extension/` 是配套的 Chrome 扩展（MV3、原生 JS、无构建），细节见其 README。

`reference/hls_downloader.py` 是早期把 m3u8 直接交给 FFmpeg 的轻量实现，仅作参考保留，不再维护。

## 免责声明

请仅下载你有权访问和保存的内容，并遵守相关网站的服务条款与版权要求。

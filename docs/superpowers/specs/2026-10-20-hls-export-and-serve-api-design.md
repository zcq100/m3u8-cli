# HLS 保留下载与 serve API 设计

日期：2026-10-20
状态：已确认

## 背景与目标

当前 `download` 只能把分片合并、封装为 MP4。需要两类增强：

1. **保留原始 HLS**：`download -f hls` 只下载播放列表与分片，不合并；源流加密时保持密文，并把密钥下载到本地，使产物成为自包含的本地 HLS 目录。
2. **远程下载服务**：新增 `serve` 子命令，在局域网启动 RESTful API，接受 Chrome 扩展提交的下载请求（含源页面请求头），异步执行并暴露任务列表与取消能力。

下载核心从 CLI 中抽离，`download` 与 `serve` 共用同一套流程，保证缓存、解密、封装语义一致。

## CLI 结构

```
m3u8-cli download <url> [-f {mp4,hls}] [-o PATH] [其余下载参数]
m3u8-cli serve [--host H] [--port P] [--token T] [--output-dir DIR]
               [--max-tasks N] [--jobs N] [--state-file FILE]
```

### download

- `-f, --format`：`mp4`（默认）或 `hls`。
- `-f mp4`：行为与产物布局完全不变；`-o` 仍为最终文件名，默认 `output.mp4`。
- `-f hls`：只下载，不调用 FFmpeg；`-o` 表示任务目录，默认 `output`。
- 其余参数（`-j / --retries / --timeout / --referer / --cookie / --user-agent / --overwrite`）语义不变。

### serve

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--token` | 自动生成 | Bearer token；未指定时随机生成并打印到启动日志 |
| `--output-dir` | 当前目录 | 任务目录根路径 |
| `--max-tasks` | `3` | 同时运行的任务数上限 |
| `--jobs` | `8` | 请求未指定 `jobs` 时的分片并发默认值 |
| `--state-file` | `<output-dir>/.m3u8-serve-state.json` | 任务状态持久化文件 |

## 下载核心

新增 `src/m3u8_cli/download.py`：

```python
@dataclass
class DownloadOptions:
    url: str
    output: Path          # mp4: 最终文件；hls: 任务目录
    format: str           # "mp4" | "hls"
    jobs: int
    retries: int
    timeout: int
    headers: dict[str, str]
    overwrite: bool

def download_hls(
    options: DownloadOptions,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    ...
```

- 流程从现有 `run_download` 迁移：解析播放列表、校验指纹、下载初始化片段、并发下载分片、按格式输出。
- 进度回调 `on_progress(completed, total)` 只统计媒体分片；CLI 用它打印原有进度行，serve 用它更新任务记录。
- 异常统一抛出 `HLSException` 及其子类，不再在核心内 `sys.exit`。
- 取消采用协作式：`Downloader` 的分片提交改为有界窗口（在途上限 `jobs * 2`），每提交一个新分片前检查 `cancel`；取消后不再补提交，已下载分片保留，最后抛出 `DownloadCancelled`。合并/导出阶段开始前也检查一次。
- `cache/state.json` 增加 `mode` 字段（`mp4` / `hls`）。指纹或模式变化时清理旧分片与初始化片段缓存，避免解密分片与密文分片混用。
- `-f mp4` 继续使用 `combine_segments()` + `mux()`；`-f hls` 不调用 FFmpeg。

`cli.run_download()` 简化为：构造 `DownloadOptions`、检查 `ffmpeg`（仅 mp4）、调用 `download_hls()`。

## `-f hls` 产物

以 `download <url> -f hls -o movie` 为例：

```
movie/
├── playlist.m3u8     # 重新生成的本地媒体播放列表
├── 000000.ts         # 原始分片；源流加密时保持密文
├── ...
├── key.bin           # 仅加密流；多个不同密钥时为 key_000.bin、key_001.bin
└── cache/            # 内部缓存与 state.json
```

- 分片文件编号 6 位零填充；播放列表中的 URI 直接使用本地文件名（无子目录）。
- 普通 TS 流分片命名为 `%06d.ts`；检测到 `EXT-X-MAP` 时视为 fMP4，输出 `init.mp4` + `%06d.m4s`，`EXT-X-MAP` 的 URI 重写为 `init.mp4`。
- 源流无加密时不生成任何密钥文件。
- 源流为 AES-128 时**不解密**：
  - 分片按服务器返回的原始字节写入缓存与产物目录；
  - 每个不同的密钥 URI 下载一次，按出现顺序命名为 `key.bin`（仅一个）或 `key_000.bin`、`key_001.bin`……；
  - `EXT-X-KEY` 的 URI 重写为对应本地文件名，`METHOD`、`IV` 等属性保留。
- 播放列表由解析结果重新生成，包含：
  - `#EXTM3U`；
  - `#EXT-X-VERSION:3`（TS）或 `#EXT-X-VERSION:6`（fMP4）；
  - `#EXT-X-TARGETDURATION`：分片时长向上取整的最大值；
  - 原始 `#EXT-X-MEDIA-SEQUENCE`：无显式 IV 时播放器按序列号推导 IV，重置会破坏解密；
  - fMP4 的 `#EXT-X-MAP`；
  - 按密钥变化插入的 `#EXT-X-KEY`（含从加密切回明文的 `METHOD=NONE`）；
  - 每个分片的 `#EXTINF` 与实际时长；
  - 结尾 `#EXT-X-ENDLIST`。
- `--overwrite` 只删除本工具生成的条目：`playlist.m3u8`、`%06d.ts`/`%06d.m4s`、`init.mp4`、`key*.bin`；目录内其他文件保留。
- 未指定 `--overwrite` 且 `playlist.m3u8` 已存在时报错。
- 导出不是整体原子操作：写入失败可能留下部分新文件，与 `convert --overwrite` 的取舍一致。

### 解析器改动

`playlist.py` 需要保留现有调用方不用的额外信息：

- 每个分片的 `EXTINF` 时长（浮点秒）；
- 原始 `EXT-X-MEDIA-SEQUENCE` 值；
- 保持现有 `key` / `map` / `sequence` / `fingerprint` 字段不变。

## serve 架构

新增 `src/m3u8_cli/serve.py`，分为三层：

1. **TaskManager**：任务创建、FIFO 调度、并发上限、状态机、取消、持久化。
2. **TaskRecord**：可序列化任务记录，字段：
   `id / url / format / output / final_path / state / created_at / started_at / finished_at / completed / total / error`。
   **不包含请求头**，也不把请求头写入状态文件。
3. **HTTP 层**：基于标准库 `ThreadingHTTPServer` + `BaseHTTPRequestHandler` 的路由、JSON 编解码、鉴权与 CORS。

### 调度与状态

- 状态机：`queued → running → succeeded | failed | cancelled`；服务重启时未完成任务标记为 `interrupted`。
- 最多 `--max-tasks` 个任务同时运行；其余按提交顺序 FIFO 排队。
- 每个运行中的任务占用一个工作线程，任务内部再由 `download_hls()` 开 `jobs` 个分片线程。
- 同一输出路径正在排队或运行时，新请求返回 409。
- 最终文件/目录已存在且请求未传 `overwrite=true` 时返回 409。
- 取消：
  - 排队任务直接移出队列并记为 `cancelled`；
  - 运行中任务设置取消事件，工作线程抛出 `DownloadCancelled` 后记为 `cancelled`；
  - 终态任务取消返回 409。
- 进度更新只改内存，持久化节流为约 2 秒一次；所有状态切换立即持久化；写入采用临时文件 + 原子替换。

### 持久化

- 文件结构：`{"version": 1, "tasks": [...]}`。
- 启动时加载；`queued` 与 `running` 一律改为 `interrupted`，不会自动重启下载。
- 只保留最近 200 条终态任务，运行中与排队任务始终保留。
- 状态文件路径默认放在 `--output-dir` 下，便于整个目录一起备份或删除。

### HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks` | 创建任务，返回 `202 {"task": {...}}` |
| `GET` | `/api/tasks` | 列表；查询参数 `state`、`limit`、`offset`，按创建时间倒序 |
| `GET` | `/api/tasks/{id}` | 任务详情 |
| `DELETE` | `/api/tasks/{id}` | 取消排队/运行中任务 |
| `OPTIONS` | `*` | CORS 预检，无需鉴权 |

`POST /api/tasks` 请求体：

```json
{
  "url": "https://example.com/playlist.m3u8",
  "format": "mp4",
  "output": "movie.mp4",
  "jobs": 8,
  "retries": 3,
  "timeout": 30,
  "overwrite": false,
  "headers": {
    "Referer": "https://example.com/",
    "Cookie": "session=xxxx",
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://example.com"
  }
}
```

- `url` 必填；`format` 默认 `mp4`；`output` 默认 `movie.mp4`（mp4）或 `output`（hls）。
- `jobs` 默认取 serve 的 `--jobs`；`retries` 默认 3；`timeout` 默认 30。
- `headers` 为字符串到字符串的映射，原样用于播放列表、分片与密钥请求。
- 响应中的 `final_path`：mp4 为最终文件绝对路径，hls 为任务目录绝对路径。
- 不提供产物下载接口；文件留在服务器上。

### 鉴权与 CORS

- 除 `OPTIONS` 外，所有 `/api` 请求要求 `Authorization: Bearer <token>`，使用 `hmac.compare_digest` 比较；失败返回 401。
- CORS 允许任意来源（Chrome 扩展 ID 不固定），允许方法与请求头为 `GET, POST, DELETE, OPTIONS` 与 `Authorization, Content-Type`；预检返回 204。

### 请求校验

- 请求体上限 1 MiB，超过返回 413。
- 非法 JSON、缺少 `url`、URL 非 `http/https`、`format` 不在白名单、数值参数越界、`headers` 非字符串映射，返回 400。
- `output` 必须是纯文件名或目录名：
  - 不含 `/`、`\`，不等于 `.` / `..`，不含 NUL；
  - 与当前排队/运行任务冲突返回 409。
- 错误响应统一为 `{"error": "..."}`。

## 模块划分

| 文件 | 变更 |
| --- | --- |
| `src/m3u8_cli/download.py` | 新增：可复用下载核心与 `DownloadOptions` |
| `src/m3u8_cli/hls_export.py` | 新增：本地 HLS 播放列表生成与文件导出 |
| `src/m3u8_cli/serve.py` | 新增：TaskManager、TaskRecord、HTTP API、`run_serve()` |
| `src/m3u8_cli/cli.py` | 修改：`download` 增加 `-f`；`run_download` 调用核心；新增 `serve` 子命令与参数校验 |
| `src/m3u8_cli/playlist.py` | 修改：保留 `EXTINF` 时长与媒体序列号 |
| `src/m3u8_cli/downloader.py` | 修改：支持不解密的原始分片下载；密钥获取可复用 |
| `src/m3u8_cli/errors.py` | 修改：新增 `DownloadCancelled` |
| `README.md` / `CLAUDE.md` | 更新命令、布局、API 与已知限制 |

依赖保持 `requests` + `pycryptodome`，不引入 Web 框架。

## 错误处理

- 核心与 CLI：继续由 `main()` 统一捕获 `HLSException` / `OSError` / `requests.RequestException` / `subprocess.CalledProcessError`；取消在 CLI 下等价于用户中断，打印提示并以非零状态退出。
- serve 任务失败：记录异常文本到 `error`，状态置 `failed`；单任务失败不影响服务与其他任务。
- serve 请求错误：按状态码返回 `{"error": ...}`；服务端异常返回 500 并记录日志。
- 找不到 `ffmpeg`：`-f mp4` 任务失败并记录；`-f hls` 不依赖 FFmpeg。

## 测试

- `tests/test_cli.py`：`download -f` 默认值与取值、`-o` 默认值随格式变化、`serve` 参数解析与非法组合。
- `tests/test_playlist.py`：`EXTINF` 时长、媒体序列号、密钥与初始化片段解析保持兼容。
- `tests/test_hls_export.py`：普通流、AES-128 单密钥/多密钥、fMP4 的播放列表内容与文件布局；`METHOD=NONE` 切换；`--overwrite` 清理范围。
- `tests/test_serve.py`：
  - `TaskManager` 注入 fake runner，覆盖并发上限、FIFO、成功/失败/取消、持久化与重启标记；
  - 在端口 0 启动真实 HTTP 服务，用 `requests` 验证鉴权、CORS、参数校验、创建/列表/详情/取消。
- 验证命令：
  - `uv run python -m unittest discover -s tests -v`
  - `uv run python -m compileall -q src`
  - 手工用本地 HTTP 服务托管 m3u8，验证 `download -f hls` 产物与 `serve` 端到端流程。

## 已知取舍

- 不提供产物下载接口，插件只能拿到服务器路径。
- 不做 SSRF 内网地址屏蔽，依赖 token 与局域网部署。
- 重启后任务标记为 `interrupted`，不自动恢复；重新提交同一输出可复用缓存续传。
- 请求头不落盘，因此无法在重启后重放任务。
- 沿用现有 HLS 限制：不支持 BYTERANGE、多个初始化片段、SAMPLE-AES/DRM；播放列表只抓取一次。
- 无 `EXT-X-MAP` 的音频流仍按 `.ts` 命名。
- `-f hls` 导出不是整体原子操作，`--overwrite` 失败可能留下部分新文件。
- 任务列表只保留最近 200 条终态记录。

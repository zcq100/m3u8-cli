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
- [uv](https://docs.astral.sh/uv/)
- [FFmpeg](https://ffmpeg.org/)（需要能在 `PATH` 中找到 `ffmpeg`）

## 安装

```bash
git clone https://git.r1a.co:5000/zcq100/m3u8_download.git
cd m3u8_download
uv sync
```

## 使用

```bash
uv run m3u8-cli download "<m3u8_url>"
```

也可以以模块方式运行：

```bash
uv run python -m m3u8_cli download "<m3u8_url>"
```

转换同样支持模块方式：

```bash
uv run python -m m3u8_cli convert movie.mp4 -o out
```

### 常用示例

```bash
# 默认输出到 output/output.mp4
uv run m3u8-cli download "https://example.com/playlist.m3u8"

# 指定输出文件名与并发数
uv run m3u8-cli download "https://example.com/playlist.m3u8" -o movie.mp4 -j 16

# 需要 Referer / Cookie 等请求头时
uv run m3u8-cli download "https://example.com/playlist.m3u8" \
  --referer "https://example.com/" \
  --cookie "session=xxxx"

# 覆盖已存在的最终文件
uv run m3u8-cli download "https://example.com/playlist.m3u8" -o movie.mp4 --overwrite
```

### 转换视频

```bash
# 默认转换为 HLS：当前目录下生成 movie/playlist.m3u8 与 movie/ts/*.ts
uv run m3u8-cli convert movie.mp4

# 指定输出目录、格式与编码
uv run m3u8-cli convert movie.mp4 -f mkv -c hevc -o out

# 转换 HLS 并设置每片 10 秒
uv run m3u8-cli convert movie.mp4 -f hls --segment-time 10

# 覆盖已有产物
uv run m3u8-cli convert movie.mp4 -f mp4 --overwrite
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `input` | 必填 | 本地视频文件路径 |
| `-f, --format` | `hls` | `hls`、`mp4`、`mkv`、`webm` |
| `-c, --codec` | `h264` | `h264`、`hevc`、`copy` |
| `-o, --output-dir` | 输入文件 stem | 输出目录 |
| `--segment-time` | `6` | 仅 HLS 使用 |
| `--overwrite` | 关闭 | 覆盖已有产物 |

HLS 输出固定为 `playlist.m3u8` 加 `ts/000000.ts` 起的分片；WebM 重编码时使用 VP9 + Opus，`-c copy` 则直接复制源编码并由 FFmpeg 判断兼容性。

- `mp4`/`mkv`/`webm` 输出为 `<输出目录>/<输入文件 stem>.<扩展名>`，例如 `m3u8-cli convert movie.mp4 -f mkv -o out` 生成 `out/movie.mkv`；单文件先写入 `.partial.*`，成功后原子替换。
- `-c copy` 时 HLS 分片边界由源关键帧决定，`--segment-time` 只是近似目标。

### 下载参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `url` | 必填 | m3u8 播放列表地址 |
| `-o, --output` | `output.mp4` | 最终文件名；产物保存在以此文件 stem 命名的任务目录中 |
| `-j, --jobs` | `8` | 并发下载线程数 |
| `--retries` | `3` | 单个分片下载失败后的重试次数 |
| `--timeout` | `30` | 网络请求超时（秒） |
| `--referer` | 无 | 自定义 `Referer` 请求头 |
| `--cookie` | 无 | 自定义 `Cookie` 请求头 |
| `--user-agent` | `Mozilla/5.0` | 自定义 `User-Agent` 请求头 |
| `--overwrite` | 关闭 | 覆盖已存在的最终文件 |

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
- HLS 转换在 `--overwrite` 时会先删除旧 `playlist.m3u8` 和 `ts/` 中 6 位编号的分片，保留其他文件；转换失败将丢失旧产物，并可能留下部分新分片。

## 开发

```bash
# 编译检查
uv run python -m compileall -q src

# 类型检查（使用 .venv）
pyright

# 单元测试
uv run python -m unittest discover -s tests -v

# 构建 wheel
uv build
```

项目目前没有 lint 或 CI 配置，单元测试使用标准库 unittest。

代码结构：

```
src/m3u8_cli/
├── cli.py           # 命令行入口与流程编排
├── convert.py       # 视频格式转换（FFmpeg）
├── playlist.py      # m3u8 解析
├── downloader.py    # 并发下载与 AES-128 解密
├── merge.py         # 分片合并与 FFmpeg 封装
├── paths.py         # 任务目录派生
├── errors.py        # 共享异常
└── __main__.py      # python -m m3u8_cli
```

`reference/hls_downloader.py` 是早期把 m3u8 直接交给 FFmpeg 的轻量实现，仅作参考保留，不再维护。

## 免责声明

请仅下载你有权访问和保存的内容，并遵守相关网站的服务条款与版权要求。

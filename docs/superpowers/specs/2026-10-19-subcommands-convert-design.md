# 子命令与 convert 设计

日期：2026-10-19

## 目标

把现有下载流程改为 `download` 子命令，新增 `convert` 子命令，用 FFmpeg 把本地视频转换为 HLS（默认）或其他常见容器格式。下载行为、任务目录布局和缓存语义保持不变。

## CLI 结构

```
m3u8-cli download <url> [原有参数]
m3u8-cli convert <input> [-f {hls,mp4,mkv,webm}] [-c {h264,hevc,copy}]
                         [-o DIR] [--segment-time N] [--overwrite]
```

- 使用 `argparse` subparsers，子命令必填。
- `download` 的参数与行为完全不变：`-o` 仍是最终文件名，其余参数保持现状。
- `convert` 参数：
  - `input`：本地视频文件路径；不存在或不是文件时报错。
  - `-f, --format`：`hls`（默认）、`mp4`、`mkv`、`webm`。
  - `-c, --codec`：`h264`（默认）、`hevc`、`copy`。
  - `-o, --output-dir`：输出目录；省略时使用输入文件的 stem 作为目录名（相对当前工作目录）。
  - `--segment-time`：HLS 分片时长（秒），默认 6，必须为正整数；非 HLS 格式忽略。
  - `--overwrite`：允许覆盖已有产物。

## 输出布局

以输入 `movie.mp4` 为例：

| 格式 | 未指定 `-o` | 指定 `-o out` |
| --- | --- | --- |
| hls | `./movie/playlist.m3u8`、`./movie/ts/000000.ts` | `out/playlist.m3u8`、`out/ts/000000.ts` |
| mp4 | `./movie/movie.mp4` | `out/movie.mp4` |
| mkv | `./movie/movie.mkv` | `out/movie.mkv` |
| webm | `./movie/movie.webm` | `out/movie.webm` |

- HLS 分片编号为 6 位零填充，从 `000000` 开始。
- 播放列表中的分片 URI 带 `ts/` 前缀（通过 `-hls_base_url ts/`），保证相对路径可被播放器解析。
- 输出目录按需递归创建；若该路径已存在但不是目录，则报错。
- 目标路径不得与输入文件相同（例如 `-f mp4 -o .` 且输入就是 `movie.mp4`），否则无论是否指定 `--overwrite` 都报错，避免覆盖源文件。
- 输入文件不得位于输出目录的 `ts/` 内，否则 `--overwrite` 清理分片时会删除输入；检测到该情况直接报错。
- 目标存在性判断：
  - HLS：`playlist.m3u8` 或 `ts/` 任一存在即视为已存在；若 `ts` 存在但不是目录，报错。
  - 单文件格式：目标文件存在即视为已存在。
- 未指定 `--overwrite` 且目标存在时报错退出。
- 指定 `--overwrite` 时：
  - HLS 先删除旧的 `playlist.m3u8` 和 `ts/` 中 6 位编号的 `.ts` 文件，再开始转换，避免残留旧分片；`ts/` 中的其他条目保留。
  - 单文件格式先写入同目录的 `<stem>.partial.<ext>`，成功后原子替换目标文件，失败时删除临时文件。
- 输出路径、临时输出路径和 `ts/` 不允许是符号链接；陈旧的 `.partial.*` 在调用 FFmpeg 前删除。

## 编码映射

默认 `-c h264`，即 H.264 视频 + AAC 音频。

| `-f` | `-c h264` | `-c hevc` | `-c copy` |
| --- | --- | --- | --- |
| hls / mp4 / mkv | libx264 + AAC | libx265 + AAC | 流复制 |
| webm | libvpx-vp9 + Opus | libvpx-vp9 + Opus | 流复制 |

- mp4 输出使用 HEVC 时追加 `-tag:v hvc1`，提升播放器兼容性。
- WebM 容器不支持 H.264/AAC，`-c h264` 与 `-c hevc` 在 WebM 下都映射为 VP9 + Opus。
- `-c copy` 不做格式兼容性探测；源编码与目标容器不兼容时由 FFmpeg 报错，程序以非零状态退出。
- 只映射视频和音频流：`-map 0:v? -map 0:a?`，不携带字幕和数据流。

## FFmpeg 调用

HLS：

```
ffmpeg -hide_banner -y -i <input> -map 0:v? -map 0:a? <编码参数>
  -f hls -hls_time <N> -hls_playlist_type vod -hls_list_size 0
  -hls_base_url ts/ -hls_segment_filename <dir>/ts/%06d.ts
  <dir>/playlist.m3u8
```

- 重编码（h264/hevc）时追加
  `-force_key_frames expr:gte(t,n_forced*<N>)`，让分片边界贴近目标时长。
- `-c copy` 时不做关键帧强制，分片边界由源关键帧决定。

单文件格式：

```
ffmpeg -hide_banner -y -i <input> -map 0:v? -map 0:a? <编码参数> <临时输出>
```

- mp4 追加 `-movflags +faststart`。
- 成功后把临时输出原子替换为目标文件。

## 模块划分

- 新增 `src/m3u8_cli/convert.py`：
  - 格式与编码白名单映射；
  - 输出路径派生与目标存在性检查；
  - FFmpeg 命令构造与执行；
  - `--overwrite` 的清理与原子替换逻辑。
- `cli.py`：
  - `build_parser()` 改为带 subparsers；
  - 现有下载流程改名为 `run_download(args)`；
  - 新增 `run_convert(args)`；
  - `main()` 负责参数校验、子命令分发与统一异常捕获。
- `paths.py`、`downloader.py`、`playlist.py`、`merge.py`、`errors.py` 不做结构性改动。
- 同步更新 `README.md` 与 `CLAUDE.md` 中的命令示例和结构说明。

## 错误处理

- 输入不存在或不是普通文件：抛出 `HLSException`。
- 目标已存在且未指定 `--overwrite`：抛出 `HLSException`。
- HLS 格式下 `--segment-time` 小于 1：`parser.error`；非 HLS 格式忽略该参数。
- 找不到 `ffmpeg`：与下载流程一致，直接退出并提示安装。
- FFmpeg 执行失败：沿用 `main()` 对 `subprocess.CalledProcessError` 的统一捕获；FFmpeg 自身错误输出直接显示。

## 验证

1. `uv run m3u8-cli download --help`、`uv run m3u8-cli convert --help`。
2. 用 `ffmpeg` 的 `testsrc` 生成测试视频。
3. 分别转换 `hls`、`mp4`、`mkv`、`webm`，检查目录结构与文件命名。
4. 用 `ffprobe` 确认 HLS 播放列表和输出文件可读。
5. 验证默认输出目录、`-o`、`--overwrite`、`-c hevc`、`-c copy` 行为。
6. `uv run python -m unittest discover -s tests -v` 全部通过。
7. `uv run python -m compileall -q src`。

## 已知取舍

- `convert` 只接受本地文件，URL 下载仍由 `download` 负责。
- 不探测源编码；`-c copy` 的兼容性由 FFmpeg 判定。
- HLS 转换不是整体原子操作；`--overwrite` 会先清理旧产物，转换失败时可能留下部分新分片。
- 不提供字幕、多音轨选择等高级映射选项。

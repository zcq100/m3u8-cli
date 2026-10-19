# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库概述

这是一个 HLS 视频下载与格式转换工具：

- `src/m3u8_cli/`：唯一维护的实现，打包为 `m3u8_cli` 包，提供 `m3u8-cli` 命令和 `python -m m3u8_cli` 两种入口。CLI 通过 argparse 子命令提供 `download`（下载 HLS）与 `convert`（转换本地视频）。下载流程并发下载分片、本地缓存/断点续传、AES-128 解密、本地合并后调用 FFmpeg 无损封装，所有下载产物写入以输出文件名派生的任务子目录。
- `reference/hls_downloader.py`：早期把 m3u8 直接交给 FFmpeg 的轻量实现，仅作参考保留，不再维护或修改。

运行环境依赖 `ffmpeg`（需在 PATH 中）和 `uv`。Python 版本固定为 3.12（见 `.python-version`）。

## 常用命令

```bash
# 安装依赖并更新入口点
uv sync

# 主下载器（并发 + 缓存）
# -o output.mp4 会生成 output/output.mp4 和 output/cache/
uv run m3u8-cli download "<m3u8_url>" -o output.mp4 -j 8
uv run python -m m3u8_cli download "<m3u8_url>" -o output.mp4 -j 8

# 需要鉴权时
uv run m3u8-cli download "<url>" --referer "<referer>" --cookie "<cookie>"

# 转换视频（默认 HLS）
uv run m3u8-cli convert movie.mp4 -o out
uv run m3u8-cli convert movie.mp4 -f mkv -c hevc -o out

# 单元测试
uv run python -m unittest discover -s tests -v

# 打包
uv build
```

仓库目前没有 lint 或格式化配置，也没有 CI；单元测试使用标准库 unittest。

## 架构

### 模块划分

- `cli.py`：argparse 子命令（`download` / `convert`）、参数校验、流程编排、`state.json` 指纹处理；`main()` 根据 `args.command` 分发到 `run_download()` / `run_convert()`，并统一捕获 `HLSException` / `requests.RequestException` / `subprocess.CalledProcessError`。
- `convert.py`：格式白名单、编码映射、输出路径派生、覆盖检查与 FFmpeg 调用。
- `playlist.py`：播放列表获取与解析。递归解析 master playlist 并选择 `BANDWIDTH` 最高的变体（最多 5 层），解析 `EXT-X-KEY` / `EXT-X-MAP`，返回分片列表和播放列表原文的 SHA-256 指纹。
- `downloader.py`：`Downloader` 负责单个 URL 的获取、重试、密钥缓存和 AES-128 解密。每个线程持有自己的 `requests.Session`（`threading.local`）；密钥按 URL 全局缓存并加锁。已存在且非空的缓存文件会被直接复用，实现断点续传；写入时先写 `.part` 再原子替换。
- `paths.py`：把 `-o` 派生为 `(final, cache_dir)`，并校验任务目录。
- `merge.py`：`combine_segments()` 按播放列表顺序拼接本地分片（fMP4 先写初始化片段）；`mux()` 对本地 `combined.*` 运行一次 FFmpeg（`-map 0 -c copy -movflags +faststart`），先输出到 `cache/*.partial.mp4`，成功后原子替换为最终文件。
- `errors.py`：共享的 `HLSException`。

### 处理流程

1. **播放列表解析** — `playlist.load_playlist()` 选择最高带宽变体并生成分片元数据。
2. **并发下载** — `downloader.Downloader` 配合 `ThreadPoolExecutor` 下载分片和初始化片段。
3. **解密** — 仅支持 `METHOD=AES-128` 且 `KEYFORMAT=identity`。未提供 `IV` 标签时，按 HLS 规范用分片媒体序列号（16 字节大端）作为 IV。
4. **合并与封装** — `merge.combine_segments()` 在本地拼接，`merge.mux()` 调用 FFmpeg 无损封装。

合并顺序以播放列表中的分片索引为准，不能用 `as_completed()` 的完成顺序。

### 转换流程

`convert.py` 先派生输出目录并检查覆盖，然后执行一次 FFmpeg 调用：HLS 写入 `playlist.m3u8` 与 `ts/%06d.ts`（播放列表通过 `-hls_base_url ts/` 引用分片）；单文件先写 `.partial.*`，成功后原子替换。`--overwrite` 的 HLS 清理发生在转换前，失败可能丢失旧产物。

### 任务目录

路径由 `paths.resolve_task_paths()` 统一派生：`-o output_2.mp4` 生成任务目录 `output_2/`，最终文件为 `output_2/output_2.mp4`，中间产物在 `output_2/cache/`。若输出路径本身已经是 `<dir>/<dir>.mp4` 形式，则复用该目录，避免重复嵌套。

任务目录布局是持久化契约，改动时必须同时更新下载、失效清理和合并三处：

```
<task>/
  <name>.mp4          # 最终产物
  cache/
    state.json        # {"fingerprint": ...}；播放列表变化时清理旧分片
    segment_%06d.bin  # 已解密的媒体分片，按播放列表索引编号
    map_%04d.bin      # fMP4 初始化片段
    combined.ts|mp4   # 合并后的中间文件
    <name>.partial.mp4
```

### 参考实现

`reference/hls_downloader.py` 通过单次 FFmpeg 调用完成下载、解密和封装，不做分片缓存，也无断点续传，且未采用任务子目录布局。不要修改此文件。

### 打包

`pyproject.toml` 使用 `uv_build` 后端，自动从 `src/` 发现 `m3u8_cli` 包；`[project.scripts]` 的 `m3u8-cli = "m3u8_cli.cli:main"` 是命令入口，`src/m3u8_cli/__main__.py` 支持 `python -m m3u8_cli`。

## 已知限制

- 不支持 `EXT-X-BYTERANGE`，也不支持带 BYTERANGE 的初始化片段。
- 只支持单个初始化片段；检测到多个不同 `EXT-X-MAP` 会直接报错。
- 只支持 AES-128（identity key format），不处理 SAMPLE-AES 或任何 DRM。
- 播放列表只抓取一次，不刷新直播/事件播放列表的滑动窗口。
- `convert` 只接受本地文件；不探测源编码；HLS 转换在 `--overwrite` 时会先删除旧产物，没有整体原子性。
- 运行产物已在 `.gitignore` 中忽略（`*.mp4`、`*.mkv`、`*.webm`、`*.m3u8`、`*.ts`、`*.part`、`*.partial.*`、`*.hls-cache/`、`**/cache/`）；不要用 `git add -f` 把任务目录、转换产物或缓存加入 Git。

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库概述

这是一个 HLS 视频下载工具：

- `main.py`：唯一维护的主入口。并发下载分片、本地缓存/断点续传、AES-128 解密、本地合并后调用 FFmpeg 无损封装。所有产物写入以输出文件名派生的任务子目录。
- `hls_downloader.py`：早期把 m3u8 直接交给 FFmpeg 的轻量实现，仅作参考保留，不再维护或修改。

运行环境依赖 `ffmpeg`（需在 PATH 中）和 `uv`。Python 版本固定为 3.12（见 `.python-version`）。

## 常用命令

```bash
# 安装依赖
uv sync

# 主下载器（并发 + 缓存）
# -o output.mp4 会生成 output/output.mp4 和 output/cache/
uv run python main.py "<m3u8_url>" -o output.mp4 -j 8

# 需要鉴权时
uv run python main.py "<url>" --referer "<referer>" --cookie "<cookie>"

# 打包（包入口目前是占位实现）
uv build
uv run download-v
```

仓库目前没有测试、lint 或格式化配置，也没有 CI。

## 架构

### main.py（主流程）

处理链分为四个阶段：

1. **播放列表解析** — `load_playlist()` 递归解析 master playlist，选择 `BANDWIDTH` 最高的变体（最多 5 层）；随后逐行解析媒体播放列表，记录每个分片的 URL、媒体序列号、当前生效的 `EXT-X-KEY` 和 `EXT-X-MAP`。返回的 `fingerprint` 是对播放列表原文的 SHA-256。
2. **并发下载** — `Downloader` 用 `ThreadPoolExecutor` 下载分片。每个线程持有自己的 `requests.Session`（`threading.local`）；密钥按 URL 全局缓存并加锁，避免重复拉取。已存在且非空的缓存文件会被直接复用，实现断点续传；写入时先写 `.part` 再原子替换。
3. **解密** — 仅支持 `METHOD=AES-128` 且 `KEYFORMAT=identity`。未提供 `IV` 标签时，按 HLS 规范用分片媒体序列号（16 字节大端）作为 IV。
4. **合并与封装** — 在本地按播放列表顺序拼接缓存分片：fMP4 先写入初始化片段再拼媒体分片，TS 直接拼接。最后只对本地 `combined.*` 运行一次 FFmpeg（`-map 0 -c copy -movflags +faststart`），先输出到 `.partial.mp4`，成功后再重命名为目标文件。

路径由 `resolve_task_paths()` 统一派生：`-o output_2.mp4` 生成任务目录 `output_2/`，最终文件为 `output_2/output_2.mp4`，中间产物在 `output_2/cache/`。若输出路径本身已经是 `<dir>/<dir>.mp4` 形式，则复用该目录，避免重复嵌套。

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

合并顺序以播放列表中的分片索引为准，不能用 `as_completed()` 的完成顺序。

### hls_downloader.py（仅参考，不再维护）

单次 FFmpeg 调用：通过 `-headers` 传入鉴权头（CRLF 分隔），开启 `-reconnect`，失败时删除输出并重试整个 FFmpeg 进程。`--reencode` 切换到 libx264/AAC，否则 `-c copy`。该脚本不做分片缓存，也无断点续传，且未采用任务子目录布局。不要修改此文件。

### 打包

`pyproject.toml` 定义了 `download-v` 命令，但 `src/download_v/__init__.py` 目前只打印占位文本，与根目录脚本没有关联。若要把下载器接入包入口，需同时修改该文件和 `[project.scripts]`。

## 已知限制

- 不支持 `EXT-X-BYTERANGE`，也不支持带 BYTERANGE 的初始化片段。
- 只支持单个初始化片段；检测到多个不同 `EXT-X-MAP` 会直接报错。
- 只支持 AES-128（identity key format），不处理 SAMPLE-AES 或任何 DRM。
- 播放列表只抓取一次，不刷新直播/事件播放列表的滑动窗口。
- 运行产物已在 `.gitignore` 中忽略（`*.mp4`、`*.part`、`*.hls-cache/`、`**/cache/`）；不要用 `git add -f` 把任务目录或缓存加入 Git。

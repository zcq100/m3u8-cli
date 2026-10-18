# 包结构重组设计

日期：2026-10-18

## 目标

把根目录 430 行的 `main.py` 拆分为 `src/m3u8_download/` 下的职责明确的模块，接入 `pyproject.toml` 的 `m3u8-download` 入口；参考脚本移入 `reference/`。CLI 行为、任务子目录布局、缓存与续传语义保持不变。

## 目标结构

```
src/m3u8_download/
├── __init__.py      # 包版本，无副作用
├── __main__.py      # python -m m3u8_download
├── cli.py           # argparse、流程编排、顶层错误处理
├── errors.py        # HLSException
├── paths.py         # 任务目录派生
├── playlist.py      # m3u8 解析、指纹
├── downloader.py    # 并发下载、密钥缓存、AES-128 解密
└── merge.py         # 分片合并 + FFmpeg 封装
reference/
└── hls_downloader.py
```

## 模块边界

- `playlist.py`：播放列表获取与解析；master playlist 选择最高带宽、解析 `EXT-X-KEY` / `EXT-X-MAP`、生成分片列表与 SHA-256 指纹。不下载分片。
- `downloader.py`：单个 URL 的获取、重试、密钥缓存、AES-128 解密、缓存文件原子写入。
- `paths.py`：把 `-o` 参数派生为 `(final, cache_dir)`，并校验任务目录。
- `merge.py`：按播放列表顺序拼接本地分片（fMP4 先写初始化片段），调用 FFmpeg 输出到 `cache/*.partial.mp4` 后原子替换。
- `cli.py`：参数解析与校验、`state.json` 指纹处理、创建 `Downloader`、编排流程，并捕获 `HLSException` / `requests.RequestException` / `subprocess.CalledProcessError`。
- `errors.py`：共享的 `HLSException`，避免子模块之间的无意义依赖。

## 入口与兼容性

- `pyproject.toml`：`m3u8-download = "m3u8_download.cli:main"`。
- 运行方式：`uv run m3u8-download ...` 或 `uv run python -m m3u8_download ...`。
- 删除根目录 `main.py`；`hls_downloader.py` 原样移动到 `reference/`，不修改内容。
- 参数、输出文案、任务目录布局、缓存与续传语义不变。

## 验证

1. 两个入口的 `--help`。
2. 本地 HLS fixture 端到端运行，确认任务目录结构，FFprobe 可读输出。
3. `uv build` 确认 wheel 包含全部子模块。
4. 更新 `CLAUDE.md`。

# 任务子目录布局设计

日期：2026-10-18

## 目标

`hls_downloader_py.py` 更名为 `main.py`，作为唯一维护的 HLS 下载入口。下载的最终产物与中间产物统一放入以输出文件名派生的任务子目录。`hls_downloader.py` 保持原样，仅作为参考实现，不再修改。

## 路径规则

```
-o output_2.mp4
→ task_dir  = <cwd>/output_2
→ final     = <cwd>/output_2/output_2.mp4
→ cache_dir = <cwd>/output_2/cache
```

- `task_dir` = 最终文件路径的父目录 + 文件名 stem。
- 幂等规则：若 `output.parent.name == output.stem`（如 `-o output_2/output_2.mp4`），直接使用该父目录，避免重复嵌套。
- 任务目录自动创建；若同名路径存在且不是目录，报错退出。
- 旧版 `<output>.hls-cache/` 不再使用，也不迁移。

## 目录布局

```
output_2/
├── output_2.mp4
└── cache/
    ├── state.json
    ├── segment_%06d.bin
    ├── map_%04d.bin
    ├── combined.ts|mp4
    └── output_2.partial.mp4
```

- 分片、初始化片段、`state.json`、合并文件和 FFmpeg 临时输出全部位于 `cache/`。
- 指纹失效逻辑不变：播放列表变化时删除 `segment_*.bin` 和 `map_*.bin`。
- 存在性检查针对任务目录内的最终文件，`--overwrite` 语义不变。
- 中断续传不变：任务目录保留时复用非空分片。

## 验证

- `python -m py_compile main.py`、`--help`。
- 本地 FFmpeg 生成 TS + m3u8，`python -m http.server` 提供访问，运行下载器确认目录结构与可播放性。
- 验证幂等路径（`-o smoke/smoke.mp4` 不会产生 `smoke/smoke/`）和 `--overwrite`。

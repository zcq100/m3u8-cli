# HLS 分片加密设计

日期：2026-10-19

## 目标

为 `convert` 子命令的 HLS 输出增加可选的 AES-128 分片加密开关。默认关闭，不改变现有行为。

## CLI

```
m3u8-cli convert <input> -f hls --encrypt [其他参数]
```

- 新增布尔参数 `--encrypt`。
- 仅允许与 `-f hls` 一起使用；其他格式时报错退出：
  `parser.error("--encrypt 仅适用于 HLS")`。
- `convert_video()` 内部同样校验，防止被其他调用方误用。
- 默认关闭。

## 产物布局

开启加密后：

```
<dir>/
├── playlist.m3u8          # 含 #EXT-X-KEY:METHOD=AES-128,URI="key.bin"
├── key.bin                # 16 字节随机密钥
├── .key_info.txt          # FFmpeg 临时输入，转换结束后删除
└── ts/000000.ts ...
```

- 密钥用 `secrets.token_bytes(16)` 生成，每次 `--encrypt` 转换都重新生成。
- 播放列表中的密钥 URI 固定为相对路径 `key.bin`，播放器按播放列表所在目录解析；分发时必须把 `key.bin` 与 `playlist.m3u8` 放在一起。
- `.key_info.txt` 内容为两行：

  ```
  key.bin
  <输出目录>/key.bin
  ```

  第一行写入播放列表，第二行供 FFmpeg 本地读取。
- 不提供 IV 输入；FFmpeg 会写入固定 `IV=0`，同一密钥与 IV 应用于所有分片（见已知取舍）。
- `.key_info.txt` 在转换成功或失败后都删除。

## FFmpeg 调用

加密时在 HLS muxer 参数中追加：

```
-hls_key_info_file <输出目录>/.key_info.txt
```

其余参数（`-hls_time`、`-hls_base_url ts/`、`-hls_segment_filename` 等）不变；`-c copy` 与重编码都支持。

## 覆盖与安全

- 开启 `--encrypt` 时，`key.bin` 与 `.key_info.txt` 纳入“目标已存在”判断：未指定 `--overwrite` 且 `playlist.m3u8`、`ts/`、`key.bin` 或 `.key_info.txt` 任一存在时报错。
- 输入文件不得是输出目录中的 `key.bin` 或 `.key_info.txt`，否则报错，避免生成的密钥产物覆盖源文件。
- `--overwrite` 时先删除旧播放列表与 6 位编号分片，生成新密钥并覆盖 `key.bin`。
- `key.bin` 是符号链接时：
  - 未指定 `--overwrite`：视为已存在，报错。
  - 指定 `--overwrite`：先删除链接本身，再写入新密钥，不跟随链接。
- `key.bin` 是目录时报错。
- `.key_info.txt` 是目录时报错；是符号链接时先删除链接再写入。
- 转换失败时删除 `.key_info.txt` 和本次新写的 `key.bin`。
- `.gitignore` 增加 `key.bin` 与 `.key_info.txt`，避免密钥被提交。

## 模块划分

- `cli.py`：`convert` 子解析器增加 `--encrypt`；校验格式并传给 `convert_video()`。
- `convert.py`：
  - `build_hls_command()` 增加 `encrypt` 参数（默认 `False`），加密时追加 `-hls_key_info_file`；
  - 新增密钥生成、key-info 写入/清理逻辑；
  - `convert_video()` 增加 `encrypt` 参数（默认 `False`），处理目标存在性、符号链接与失败清理。
- 其他模块不变。

## 错误处理

- 非 HLS 格式使用 `--encrypt`：`parser.error`。
- `key.bin` 已是目录：`HLSException`。
- 密钥或 key-info 写入失败：`OSError`，由 `main()` 统一显示为 `错误：...`。
- FFmpeg 失败：沿用 `CalledProcessError` 捕获；失败前清理临时 key-info 和新密钥。

## 测试

1. 单元测试（mock `subprocess.run`）：
   - `build_hls_command(..., encrypt=True)` 包含 `-hls_key_info_file`，默认不含；
   - `--encrypt` 转换后 `key.bin` 为 16 字节，`.key_info.txt` 内容正确且已删除；
   - 非 HLS + `encrypt=True` 抛出 `HLSException`；
   - 未指定 `--overwrite` 且 `key.bin` 已存在时报错；
   - `--overwrite` 连续两次生成不同密钥；
   - `key.bin` 为符号链接时的拒绝/替换行为；
   - 转换失败后 `.key_info.txt` 与 `key.bin` 均不存在。
2. 集成测试（真实 ffmpeg/ffprobe）：
   - `--encrypt` 转换后播放列表包含
     `#EXT-X-KEY:METHOD=AES-128,URI="key.bin"`；
   - `key.bin` 为 16 字节；
   - `ffprobe` 能读取加密后的播放列表（必须传 `-allowed_extensions ALL`，否则默认拒绝 `key.bin` 扩展名）。
3. 全量 `uv run python -m unittest discover -s tests -v` 通过。

## 文档

- README：参数表增加 `--encrypt`；输出布局增加 `key.bin`；说明密钥必须与播放列表一起分发。
- CLAUDE.md：转换流程补充加密产物与临时文件清理。
- `.gitignore`：增加密钥与 key-info 忽略规则。

## 已知取舍

- 密钥与播放列表同目录分发，这是标准 HLS AES-128 的常见做法，不是 DRM；能访问 `key.bin` 的客户端即可解密。
- 不提供自定义密钥、密钥 URI 或 IV 的参数。
- FFmpeg 的 HLS muxer 会写入固定的 `IV=0`，所有分片共用同一密钥与 IV；这是该实现方式的已知限制。
- FFmpeg/ffprobe 默认拒绝 `key.bin` 扩展名，本地播放或探测需加 `-allowed_extensions ALL`；浏览器播放器不受此限制。
- 不实现 SAMPLE-AES 或任何 DRM。

# HLS 分片加密实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `convert` 的 HLS 输出增加 `--encrypt` 开关，用 FFmpeg 的 `-hls_key_info_file` 对分片做 AES-128 加密。

**Architecture:** `convert.py` 生成随机 16 字节密钥与临时 key-info 文件，把 `-hls_key_info_file` 追加到现有 HLS 命令；`cli.py` 只负责解析 `--encrypt`、校验仅用于 HLS 并透传。转换结束后清理临时文件。

**Tech Stack:** Python 3.12、secrets、subprocess、FFmpeg HLS muxer、unittest

---

## 文件结构

- 修改：`src/m3u8_cli/convert.py` — `build_hls_command` 加密参数、密钥/key-info 写入与清理、`convert_video` 加密分支
- 修改：`src/m3u8_cli/cli.py` — `--encrypt` 参数、格式校验、透传
- 修改：`tests/test_convert.py` — 命令构造、密钥生命周期、失败清理、集成测试
- 修改：`tests/test_cli.py` — 参数解析与 main 校验
- 修改：`README.md`、`CLAUDE.md`、`.gitignore`

---

### Task 1: 为 HLS 命令添加加密参数

**Files:**
- Modify: `src/m3u8_cli/convert.py`
- Test: `tests/test_convert.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_convert.py` 的 `CommandTests` 类末尾追加：

```python
    def test_hls_encrypt_adds_key_info_file(self):
        command = build_hls_command(
            Path("/videos/movie.mp4"), Path("/out"), "h264", 6,
            encrypt=True,
        )
        self.assertIn("-hls_key_info_file", command)
        index = command.index("-hls_key_info_file")
        self.assertEqual(command[index + 1], "/out/.key_info.txt")

    def test_hls_without_encrypt_has_no_key_info(self):
        command = build_hls_command(
            Path("/videos/movie.mp4"), Path("/out"), "h264", 6
        )
        self.assertNotIn("-hls_key_info_file", command)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_convert.CommandTests -v`
Expected: ERROR，`build_hls_command() got an unexpected keyword argument 'encrypt'`

- [ ] **Step 3: 修改 build_hls_command**

把 `src/m3u8_cli/convert.py` 的 `build_hls_command()` 替换为：

```python
def build_hls_command(input_path, output_dir, codec, segment_time,
                      encrypt=False):
    """构造 HLS 转换的 FFmpeg 命令。"""
    command = [
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(input_path),
        "-map", "0:v?", "-map", "0:a?",
        *codec_args("hls", codec),
    ]

    if codec != "copy":
        command += [
            "-force_key_frames",
            f"expr:gte(t,n_forced*{segment_time})",
        ]

    command += [
        "-f", "hls",
        "-hls_time", str(segment_time),
        "-hls_playlist_type", "vod",
        "-hls_list_size", "0",
        "-hls_base_url", "ts/",
    ]

    if encrypt:
        command += [
            "-hls_key_info_file",
            str(output_dir / ".key_info.txt"),
        ]

    command += [
        "-hls_segment_filename", str(output_dir / "ts" / "%06d.ts"),
        str(output_dir / "playlist.m3u8"),
    ]
    return command
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_convert.CommandTests -v`
Expected: 6 tests OK。

全量：`uv run python -m unittest discover -s tests -v`
Expected: `Ran 28 tests ... OK`

- [ ] **Step 5: 提交**

```bash
git add src/m3u8_cli/convert.py tests/test_convert.py
git commit -m "为 HLS 命令添加加密参数"
```

---

### Task 2: 实现密钥生成与加密转换

**Files:**
- Modify: `src/m3u8_cli/convert.py`
- Test: `tests/test_convert.py`

- [ ] **Step 1: 写失败测试**

在 `ConvertVideoTests` 类末尾追加：

```python
    @mock.patch("m3u8_cli.convert.subprocess.run")
    def test_encrypt_writes_key_and_cleans_key_info(self, run):
        out = self.dir / "out"

        convert_video(
            self.input, "hls", "h264", str(out), 6, False, True
        )

        self.assertEqual(len((out / "key.bin").read_bytes()), 16)
        self.assertFalse((out / ".key_info.txt").exists())
        command = run.call_args.args[0]
        self.assertIn("-hls_key_info_file", command)

    def test_encrypt_requires_hls(self):
        with self.assertRaises(HLSException):
            convert_video(
                self.input, "mp4", "h264", str(self.dir / "out"), 6,
                False, True,
            )

    def test_encrypt_existing_key_without_overwrite(self):
        out = self.dir / "out"
        out.mkdir()
        (out / "key.bin").write_bytes(b"x" * 16)

        with self.assertRaises(HLSException):
            convert_video(
                self.input, "hls", "h264", str(out), 6, False, True
            )

    @mock.patch("m3u8_cli.convert.subprocess.run")
    def test_encrypt_overwrite_generates_new_key(self, run):
        out = self.dir / "out"
        out.mkdir()
        (out / "key.bin").write_bytes(b"old-key")

        convert_video(
            self.input, "hls", "h264", str(out), 6, True, True
        )
        first = (out / "key.bin").read_bytes()

        convert_video(
            self.input, "hls", "h264", str(out), 6, True, True
        )
        second = (out / "key.bin").read_bytes()

        self.assertEqual(len(first), 16)
        self.assertEqual(len(second), 16)
        self.assertNotEqual(first, second)

    def test_encrypt_key_symlink_without_overwrite_rejected(self):
        out = self.dir / "out"
        out.mkdir()
        target = self.dir / "real-key.bin"
        target.write_bytes(b"x" * 16)
        (out / "key.bin").symlink_to(target)

        with self.assertRaises(HLSException):
            convert_video(
                self.input, "hls", "h264", str(out), 6, False, True
            )

    @mock.patch("m3u8_cli.convert.subprocess.run")
    def test_encrypt_overwrite_replaces_key_symlink(self, run):
        out = self.dir / "out"
        out.mkdir()
        target = self.dir / "real-key.bin"
        target.write_bytes(b"target")
        (out / "key.bin").symlink_to(target)

        convert_video(
            self.input, "hls", "h264", str(out), 6, True, True
        )

        key = out / "key.bin"
        self.assertFalse(key.is_symlink())
        self.assertEqual(len(key.read_bytes()), 16)
        self.assertEqual(target.read_bytes(), b"target")

    def test_encrypt_failure_cleans_key_and_key_info(self):
        out = self.dir / "out"

        def fake_run(command, check):
            self.assertTrue((out / ".key_info.txt").exists())
            raise subprocess.CalledProcessError(1, command)

        with mock.patch(
            "m3u8_cli.convert.subprocess.run", side_effect=fake_run
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                convert_video(
                    self.input, "hls", "h264", str(out), 6, False, True
                )

        self.assertFalse((out / "key.bin").exists())
        self.assertFalse((out / ".key_info.txt").exists())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_convert.ConvertVideoTests -v`
Expected: ERROR，`convert_video() takes 6 positional arguments but 7 were given`

- [ ] **Step 3: 实现密钥与加密流程**

在 `src/m3u8_cli/convert.py` 顶部加入 `import secrets`，并在 `FORMAT_SUFFIX` 之前加入常量：

```python
HLS_KEY_NAME = "key.bin"
HLS_KEY_INFO_NAME = ".key_info.txt"
HLS_KEY_SIZE = 16
```

在 `_clear_hls_output()` 之后、`convert_video()` 之前加入：

```python
def _write_hls_key(output_dir):
    """生成新的 AES-128 密钥并返回其路径。"""
    key_path = output_dir / HLS_KEY_NAME
    if key_path.is_symlink():
        key_path.unlink()
    elif key_path.exists() and key_path.is_dir():
        raise HLSException(f"密钥路径是目录：{key_path}")
    key_path.write_bytes(secrets.token_bytes(HLS_KEY_SIZE))
    return key_path


def _write_key_info(output_dir, key_path):
    """写入 FFmpeg 的 key-info 临时文件并返回其路径。"""
    key_info = output_dir / HLS_KEY_INFO_NAME
    if key_info.is_symlink():
        key_info.unlink()
    elif key_info.exists() and key_info.is_dir():
        raise HLSException(f"密钥信息路径是目录：{key_info}")
    key_info.write_text(
        f"{HLS_KEY_NAME}\n{key_path}\n",
        encoding="utf-8",
    )
    return key_info
```

把 `convert_video()` 替换为：

```python
def convert_video(input_arg, fmt, codec, output_arg,
                  segment_time, overwrite, encrypt=False):
    """转换视频并返回最终产物路径。"""
    if encrypt and fmt != "hls":
        raise HLSException("--encrypt 仅适用于 HLS")

    input_path = Path(input_arg).expanduser().resolve()
    if not input_path.is_file():
        raise HLSException(f"输入文件不存在：{input_path}")

    output_dir = resolve_output_dir(input_path, output_arg)

    if fmt == "hls":
        playlist = output_dir / "playlist.m3u8"
        segments_dir = output_dir / "ts"
        key_path = output_dir / HLS_KEY_NAME

        if playlist == input_path:
            raise HLSException(f"输出文件与输入文件相同：{playlist}")
        if segments_dir == input_path or segments_dir in input_path.parents:
            raise HLSException(f"输入文件位于分片目录中：{input_path}")
        if segments_dir.is_symlink():
            raise HLSException(f"分片路径是符号链接：{segments_dir}")
        if segments_dir.exists() and not segments_dir.is_dir():
            raise HLSException(f"分片路径不是目录：{segments_dir}")
        if playlist.is_dir():
            raise HLSException(f"播放列表路径不是文件：{playlist}")
        if not overwrite and (
            playlist.exists()
            or playlist.is_symlink()
            or segments_dir.exists()
            or (encrypt and (key_path.exists() or key_path.is_symlink()))
        ):
            raise HLSException(
                f"目标已存在：{output_dir}，使用 --overwrite 覆盖"
            )

        if overwrite:
            _clear_hls_output(playlist, segments_dir)
        segments_dir.mkdir(parents=True, exist_ok=True)

        key_info = None
        if encrypt:
            key_path = _write_hls_key(output_dir)
            try:
                key_info = _write_key_info(output_dir, key_path)
            except Exception:
                key_path.unlink(missing_ok=True)
                raise
        else:
            key_path = None

        try:
            subprocess.run(
                build_hls_command(
                    input_path, output_dir, codec, segment_time, encrypt
                ),
                check=True,
            )
        except Exception:
            if key_info is not None:
                key_info.unlink(missing_ok=True)
            if key_path is not None:
                key_path.unlink(missing_ok=True)
            raise

        if key_info is not None:
            key_info.unlink(missing_ok=True)
        return playlist

    output = output_dir / (input_path.stem + FORMAT_SUFFIX[fmt])
    if output == input_path:
        raise HLSException(f"输出文件与输入文件相同：{output}")
    if output.is_dir():
        raise HLSException(f"输出路径是目录：{output}")
    if not overwrite and (output.exists() or output.is_symlink()):
        raise HLSException(
            f"输出文件已存在：{output}，使用 --overwrite 覆盖"
        )

    partial = output_dir / (
        input_path.stem + ".partial" + FORMAT_SUFFIX[fmt]
    )
    if partial.is_symlink():
        partial.unlink()
    elif partial.exists():
        if partial.is_dir():
            raise HLSException(f"临时输出路径是目录：{partial}")
        partial.unlink()

    try:
        subprocess.run(
            build_file_command(input_path, fmt, codec, partial),
            check=True,
        )
        partial.replace(output)
    except Exception:
        if partial.is_file() or partial.is_symlink():
            partial.unlink()
        raise
    return output
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_convert.ConvertVideoTests -v`
Expected: 22 tests OK。

全量：`uv run python -m unittest discover -s tests -v`
Expected: `Ran 36 tests ... OK`

- [ ] **Step 5: 提交**

```bash
git add src/m3u8_cli/convert.py tests/test_convert.py
git commit -m "实现 HLS 分片加密"
```

---

### Task 3: 接入 --encrypt 开关

**Files:**
- Modify: `src/m3u8_cli/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: 写失败测试**

把 `tests/test_cli.py` 的 import 区替换为：

```python
import unittest
from unittest import mock

from m3u8_cli.cli import build_parser, main
```

在 `ConvertParserTests.test_convert_defaults` 中加入：

```python
        self.assertFalse(args.encrypt)
```

在 `ConvertParserTests` 类末尾追加：

```python
    def test_convert_encrypt_flag(self):
        args = build_parser().parse_args(
            ["convert", "movie.mp4", "--encrypt"]
        )
        self.assertTrue(args.encrypt)
```

在 `if __name__ == "__main__":` 之前追加：

```python
class MainValidationTests(unittest.TestCase):
    def test_encrypt_rejected_for_non_hls(self):
        with mock.patch(
            "sys.argv",
            [
                "m3u8-cli", "convert", "movie.mp4",
                "-f", "mp4", "--encrypt",
            ],
        ):
            with self.assertRaises(SystemExit):
                main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_cli -v`
Expected: FAIL，`convert` 解析结果没有 `encrypt` 属性 / `--encrypt` 不是合法参数。

- [ ] **Step 3: 实现 CLI 接入**

在 `src/m3u8_cli/cli.py` 的 `convert.add_argument("--overwrite", ...)` 之后加入：

```python
    convert.add_argument(
        "--encrypt", action="store_true",
        help="为 HLS 分片启用 AES-128 加密",
    )
```

在 `run_convert()` 的 `convert_video(...)` 调用末尾追加 `args.encrypt`：

```python
    output = convert_video(
        args.input,
        args.format,
        args.codec,
        args.output_dir,
        args.segment_time,
        args.overwrite,
        args.encrypt,
    )
```

把 `main()` 的 convert 分支改为：

```python
    elif args.command == "convert":
        if args.encrypt and args.format != "hls":
            parser.error("--encrypt 仅适用于 HLS")
        if args.format == "hls" and args.segment_time < 1:
            parser.error("segment-time 必须为正整数")
        handler = run_convert
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest discover -s tests -v`
Expected: `Ran 38 tests ... OK`

再运行：

```bash
uv run m3u8-cli convert --help
uv run m3u8-cli convert movie.mp4 -f mp4 --encrypt
```

Expected: 帮助包含 `--encrypt`；第二条命令以非零状态退出并提示 `--encrypt 仅适用于 HLS`。

- [ ] **Step 5: 提交**

```bash
git add src/m3u8_cli/cli.py tests/test_cli.py
git commit -m "接入 HLS 加密开关"
```

---

### Task 4: 添加加密集成测试

**Files:**
- Test: `tests/test_convert.py`

- [ ] **Step 1: 写测试**

在 `HlsIntegrationTests` 类末尾追加：

```python
    def test_encrypt_playlist_references_key(self):
        """实际调用 FFmpeg，确认播放列表写入 AES-128 密钥信息。"""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            sample = work / "sample.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-y",
                    "-f", "lavfi",
                    "-i", "testsrc2=size=160x120:rate=25",
                    "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(sample),
                ],
                check=True,
                capture_output=True,
            )

            out = work / "out"
            convert_video(sample, "hls", "h264", str(out), 1, True, True)

            playlist = out / "playlist.m3u8"
            self.assertIn(
                '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"',
                playlist.read_text(),
            )
            self.assertEqual(len((out / "key.bin").read_bytes()), 16)
            self.assertFalse((out / ".key_info.txt").exists())

            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-allowed_extensions", "ALL",
                    "-show_entries", "format=format_name",
                    "-of", "default=nw=1",
                    str(playlist),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("hls", probe.stdout)
```

- [ ] **Step 2: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_convert.HlsIntegrationTests -v`
Expected: 2 tests OK

全量：`uv run python -m unittest discover -s tests -v`
Expected: `Ran 39 tests ... OK`

- [ ] **Step 3: 提交**

```bash
git add tests/test_convert.py
git commit -m "添加 HLS 加密集成测试"
```

---

### Task 5: 更新文档与忽略规则

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `.gitignore`

- [ ] **Step 1: 更新 README**

在 convert 参数表中追加一行：

```
| `--encrypt` | 关闭 | 仅 HLS；生成 `key.bin` 并加密分片 |
```

在 WebM/`-c copy` 说明之后追加：

```
- `--encrypt` 仅用于 HLS：输出目录会额外生成 16 字节的 `key.bin`，播放列表通过相对 URI 引用；分发时必须与 `playlist.m3u8` 放在一起。
```

在「已知限制」中追加：

```
- HLS AES-128 加密不是 DRM；能访问 `key.bin` 的客户端即可解密。
```

- [ ] **Step 2: 更新 CLAUDE.md**

把转换流程段落中的产物描述改为包含加密产物。当前句子：

```
`convert.py` 先派生输出目录并检查覆盖，然后执行一次 FFmpeg 调用：HLS 写入 `playlist.m3u8` 与 `ts/%06d.ts`（播放列表通过 `-hls_base_url ts/` 引用分片）；单文件先写 `.partial.*`，成功后原子替换。
```

改为：

```
`convert.py` 先派生输出目录并检查覆盖，然后执行一次 FFmpeg 调用：HLS 写入 `playlist.m3u8` 与 `ts/%06d.ts`（播放列表通过 `-hls_base_url ts/` 引用分片）；`--encrypt` 时额外生成 16 字节 `key.bin`，并通过临时 `.key_info.txt` 交给 `-hls_key_info_file` 做 AES-128 加密，转换结束删除临时文件；单文件先写 `.partial.*`，成功后原子替换。
```

- [ ] **Step 3: 更新 .gitignore**

在产物区块中追加：

```
key.bin
.key_info.txt
```

- [ ] **Step 4: 验证**

Run:

```bash
git check-ignore -v key.bin .key_info.txt
grep -n "encrypt" README.md CLAUDE.md
```

Expected: 两个文件被忽略；README/CLAUDE 中出现 `--encrypt` 说明。

- [ ] **Step 5: 提交**

```bash
git add README.md CLAUDE.md .gitignore
git commit -m "更新 HLS 加密文档与忽略规则"
```

---

## 执行期间的修正

- FFmpeg/ffprobe 默认拒绝 `key.bin` 扩展名，集成测试与端到端验证统一加 `-allowed_extensions ALL`。
- FFmpeg 的 HLS muxer 写入固定 `IV=0`，所有分片共用同一密钥与 IV，已记录为已知限制。
- `key.bin` / `.key_info.txt` 的目录形态在破坏性清理前校验，避免 `--overwrite` 先删旧产物再报错。
- key-info 清理移入 `finally`，覆盖 `KeyboardInterrupt`。
- 测试总数因新增“清理前校验”用例增加 1：Task 2 后为 36，Task 3 后为 38，Task 4 后为 39。

---

### Task 6: 端到端验证

**Files:**
- 无代码改动；发现问题时回到对应任务修复

- [ ] **Step 1: 生成测试视频**

```bash
rm -rf /tmp/m3u8-cli-encrypt-e2e
mkdir -p /tmp/m3u8-cli-encrypt-e2e
ffmpeg -hide_banner -y \
  -f lavfi -i testsrc2=size=320x240:rate=25 \
  -f lavfi -i sine=frequency=440 \
  -t 4 -c:v libx264 -c:a aac -shortest \
  /tmp/m3u8-cli-encrypt-e2e/sample.mp4
```

- [ ] **Step 2: 加密转换并检查产物**

```bash
cd /tmp/m3u8-cli-encrypt-e2e
uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4 --encrypt
find sample -type f | sort
head -8 sample/playlist.m3u8
wc -c sample/key.bin
test ! -e sample/.key_info.txt && echo "key_info cleaned"
ffprobe -v error -allowed_extensions ALL -show_entries format=format_name \
  -of default=nw=1 sample/playlist.m3u8
```

Expected:
- `sample/playlist.m3u8`、`sample/key.bin`、`sample/ts/000000.ts`
- 播放列表包含 `#EXT-X-KEY:METHOD=AES-128,URI="key.bin"`
- `key.bin` 为 16 字节
- `.key_info.txt` 不存在
- ffprobe 报告 `format_name=hls`

- [ ] **Step 3: 覆盖与错误路径**

```bash
uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4 --encrypt
# 预期：非零退出，目标已存在

cp sample/key.bin /tmp/first-key.bin
uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4 --encrypt --overwrite
cmp -s sample/key.bin /tmp/first-key.bin && echo "KEY REUSED (BUG)" || echo "new key generated"

uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4 -f mp4 --encrypt
# 预期：非零退出，提示 "--encrypt 仅适用于 HLS"
```

- [ ] **Step 4: 最终检查**

```bash
cd /home/zcq100/Documents/projects/m3u8_download
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src
git status --short
```

Expected: 38 tests OK；compileall 无输出；工作区干净。

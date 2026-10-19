# 子命令与 convert 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把下载流程改为 `download` 子命令，并新增 `convert` 子命令，用 FFmpeg 把本地视频转换为 HLS（默认）或 mp4/mkv/webm。

**Architecture:** `cli.py` 使用 argparse subparsers 分发；`convert.py` 独立负责格式白名单、编码映射、路径派生、覆盖检查和 FFmpeg 调用。下载流程整体移入 `run_download()`，行为不变。

**Tech Stack:** Python 3.12、argparse、subprocess、FFmpeg、unittest（标准库）

---

## 文件结构

- 修改：`src/m3u8_cli/cli.py` — subparsers、`run_download()`、`run_convert()`、`main()` 分发与校验
- 新建：`src/m3u8_cli/convert.py` — 转换格式、编码映射、输出路径与执行
- 新建：`tests/test_cli.py` — 子命令解析测试
- 新建：`tests/test_convert.py` — 路径派生、命令构造、覆盖语义测试
- 修改：`README.md`、`CLAUDE.md` — 命令与结构说明

---

### Task 1: 把下载流程改为 download 子命令

**Files:**
- Modify: `src/m3u8_cli/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cli.py`：

```python
import unittest

from m3u8_cli.cli import build_parser


class DownloadParserTests(unittest.TestCase):
    def test_download_subcommand(self):
        args = build_parser().parse_args(
            ["download", "https://example.com/a.m3u8"]
        )
        self.assertEqual(args.command, "download")
        self.assertEqual(args.url, "https://example.com/a.m3u8")
        self.assertEqual(args.output, "output.mp4")
        self.assertEqual(args.jobs, 8)

    def test_subcommand_required(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest discover -s tests -v`
Expected: FAIL / ERROR，`args` 没有 `command` 属性，且空参数不会触发 `SystemExit`。

- [ ] **Step 3: 实现 subparsers**

把 `src/m3u8_cli/cli.py` 的 `build_parser()` 替换为：

```python
def build_parser():
    parser = argparse.ArgumentParser(
        prog="m3u8-cli",
        description="HLS 下载与视频格式转换工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser(
        "download",
        help="下载 HLS（m3u8）视频",
        description="并发下载 HLS 分片，产物保存在任务子目录中",
    )
    download.add_argument("url", help="m3u8 地址")
    download.add_argument(
        "-o", "--output", default="output.mp4",
        help="最终文件名；产物保存在以文件 stem 命名的任务目录中",
    )
    download.add_argument("-j", "--jobs", type=int, default=8)
    download.add_argument("--retries", type=int, default=3)
    download.add_argument("--timeout", type=int, default=30)
    download.add_argument("--referer")
    download.add_argument("--cookie")
    download.add_argument("--user-agent", default="Mozilla/5.0")
    download.add_argument(
        "--overwrite", action="store_true",
        help="覆盖已有输出文件",
    )
    return parser
```

把原来的 `run(args)` 重命名为 `run_download(args)`，函数体保持不变。

把 `main()` 替换为：

```python
def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "download":
        if args.jobs < 1 or args.timeout < 1 or args.retries < 0:
            parser.error("jobs/timeout 必须为正数，retries 不能为负数")
        handler = run_download

    try:
        handler(args)
    except (HLSException, requests.RequestException,
            subprocess.CalledProcessError) as exc:
        sys.exit(f"\n错误：{exc}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest discover -s tests -v`
Expected: `Ran 2 tests ... OK`

再运行：`uv run m3u8-cli download --help`
Expected: 正常显示下载参数帮助。

- [ ] **Step 5: 提交**

```bash
git add src/m3u8_cli/cli.py tests/test_cli.py
git commit -m "把下载流程改为 download 子命令"
```

---

### Task 2: 实现转换路径派生与 FFmpeg 命令构造

**Files:**
- Create: `src/m3u8_cli/convert.py`
- Test: `tests/test_convert.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_convert.py`：

```python
import os
import tempfile
import unittest
from pathlib import Path

from m3u8_cli.convert import (
    build_file_command,
    build_hls_command,
    resolve_output_dir,
)
from m3u8_cli.errors import HLSException


class OutputDirTests(unittest.TestCase):
    def test_default_uses_input_stem(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            os.chdir(work)
            try:
                input_path = work / "movie.mp4"
                input_path.write_bytes(b"x")
                self.assertEqual(
                    resolve_output_dir(input_path, None),
                    work / "movie",
                )
                self.assertTrue((work / "movie").is_dir())
            finally:
                os.chdir(old_cwd)

    def test_custom_output_dir_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "out"
            self.assertEqual(
                resolve_output_dir(Path("movie.mp4"), str(out)),
                out,
            )
            self.assertTrue(out.is_dir())

    def test_output_path_is_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out"
            target.write_bytes(b"x")
            with self.assertRaises(HLSException):
                resolve_output_dir(Path("movie.mp4"), str(target))


class CommandTests(unittest.TestCase):
    def test_hls_h264_forces_keyframes(self):
        command = build_hls_command(
            Path("/videos/movie.mp4"), Path("/out"), "h264", 6
        )
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertIn("-force_key_frames", command)
        self.assertIn("expr:gte(t,n_forced*6)", command)
        self.assertIn("/out/ts/%06d.ts", command)

    def test_hls_copy_does_not_force_keyframes(self):
        command = build_hls_command(
            Path("/videos/movie.mp4"), Path("/out"), "copy", 6
        )
        self.assertIn("copy", command)
        self.assertNotIn("-force_key_frames", command)

    def test_webm_uses_vp9_opus(self):
        command = build_file_command(
            Path("/videos/movie.mp4"), "webm", "h264",
            Path("/out/movie.partial.webm"),
        )
        self.assertIn("libvpx-vp9", command)
        self.assertIn("libopus", command)
        self.assertNotIn("libx264", command)

    def test_mp4_hevc_uses_hvc1_tag(self):
        command = build_file_command(
            Path("/videos/movie.mp4"), "mp4", "hevc",
            Path("/out/movie.partial.mp4"),
        )
        self.assertIn("libx265", command)
        self.assertIn("-tag:v", command)
        self.assertIn("hvc1", command)
        self.assertIn("+faststart", command)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_convert -v`
Expected: ERROR，`ModuleNotFoundError: No module named 'm3u8_cli.convert'`

- [ ] **Step 3: 实现 convert.py 的纯函数部分**

创建 `src/m3u8_cli/convert.py`：

```python
"""视频格式转换（FFmpeg）。"""

from pathlib import Path

from .errors import HLSException

FORMAT_SUFFIX = {
    "hls": None,
    "mp4": ".mp4",
    "mkv": ".mkv",
    "webm": ".webm",
}

CODEC_ARGS = {
    "h264": ["-c:v", "libx264", "-c:a", "aac"],
    "hevc": ["-c:v", "libx265", "-c:a", "aac"],
    "copy": ["-c", "copy"],
}

WEBM_CODEC_ARGS = {
    # WebM 不支持 H.264/AAC，重编码统一使用 VP9/Opus。
    "h264": [
        "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
        "-c:a", "libopus",
    ],
    "hevc": [
        "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
        "-c:a", "libopus",
    ],
    "copy": ["-c", "copy"],
}


def resolve_output_dir(input_path, output_arg):
    """返回输出目录；未指定时使用输入文件 stem 作为目录名。"""
    if output_arg:
        output_dir = Path(output_arg).expanduser().resolve()
    else:
        output_dir = (Path.cwd() / input_path.stem).resolve()

    if output_dir.exists() and not output_dir.is_dir():
        raise HLSException(f"输出路径不是目录：{output_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def codec_args(fmt, codec):
    """返回目标格式对应的 FFmpeg 编码参数。"""
    if fmt == "webm":
        return WEBM_CODEC_ARGS[codec]
    return CODEC_ARGS[codec]


def build_hls_command(input_path, output_dir, codec, segment_time):
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
        "-hls_segment_filename", str(output_dir / "ts" / "%06d.ts"),
        str(output_dir / "playlist.m3u8"),
    ]
    return command


def build_file_command(input_path, fmt, codec, partial):
    """构造单文件格式转换的 FFmpeg 命令。"""
    command = [
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(input_path),
        "-map", "0:v?", "-map", "0:a?",
        *codec_args(fmt, codec),
    ]

    if fmt == "mp4":
        command += ["-movflags", "+faststart"]
        if codec == "hevc":
            command += ["-tag:v", "hvc1"]

    command.append(str(partial))
    return command
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_convert -v`
Expected: `Ran 7 tests ... OK`

- [ ] **Step 5: 提交**

```bash
git add src/m3u8_cli/convert.py tests/test_convert.py
git commit -m "添加转换路径派生与 FFmpeg 命令构造"
```

---

### Task 3: 实现转换执行与覆盖语义

**Files:**
- Modify: `src/m3u8_cli/convert.py`
- Test: `tests/test_convert.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_convert.py` 的 import 中加入 `mock` 和 `convert_video`：

```python
from unittest import mock

from m3u8_cli.convert import (
    build_file_command,
    build_hls_command,
    convert_video,
    resolve_output_dir,
)
```

在文件末尾、`if __name__ == "__main__":` 之前追加：

```python
class ConvertVideoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.input = self.dir / "movie.mp4"
        self.input.write_bytes(b"fake")

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_input(self):
        with self.assertRaises(HLSException):
            convert_video(
                self.dir / "missing.mp4", "hls", "h264", None, 6, False
            )

    def test_hls_existing_target_without_overwrite(self):
        out = self.dir / "out"
        out.mkdir()
        (out / "playlist.m3u8").write_text("old", encoding="utf-8")
        with self.assertRaises(HLSException):
            convert_video(self.input, "hls", "h264", str(out), 6, False)

    def test_ts_path_is_file(self):
        out = self.dir / "out"
        out.mkdir()
        (out / "ts").write_bytes(b"not a dir")
        with self.assertRaises(HLSException):
            convert_video(self.input, "hls", "h264", str(out), 6, True)

    @mock.patch("m3u8_cli.convert.subprocess.run")
    def test_hls_overwrite_clears_old_segments(self, run):
        out = self.dir / "out"
        (out / "ts").mkdir(parents=True)
        (out / "ts" / "000000.ts").write_bytes(b"old")
        (out / "playlist.m3u8").write_text("old", encoding="utf-8")

        result = convert_video(
            self.input, "hls", "h264", str(out), 6, True
        )

        self.assertEqual(result, out / "playlist.m3u8")
        self.assertFalse((out / "ts" / "000000.ts").exists())
        self.assertTrue((out / "ts").is_dir())
        run.assert_called_once()

    @mock.patch("m3u8_cli.convert.subprocess.run")
    def test_single_file_uses_partial_then_replace(self, run):
        def fake_run(command, check):
            Path(command[-1]).write_bytes(b"new")

        run.side_effect = fake_run
        out = self.dir / "out"

        result = convert_video(
            self.input, "mkv", "h264", str(out), 6, False
        )

        self.assertEqual(result, out / "movie.mkv")
        self.assertEqual(result.read_bytes(), b"new")
        self.assertFalse((out / "movie.partial.mkv").exists())

    def test_output_equals_input_rejected(self):
        with self.assertRaises(HLSException):
            convert_video(
                self.input, "mp4", "h264", str(self.dir), 6, True
            )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_convert -v`
Expected: ERROR，`ImportError: cannot import name 'convert_video'`

- [ ] **Step 3: 实现 convert_video**

在 `src/m3u8_cli/convert.py` 顶部补充导入：

```python
import shutil
import subprocess
```

在文件末尾追加：

```python
def convert_video(input_arg, fmt, codec, output_arg,
                  segment_time, overwrite):
    """转换视频并返回最终产物路径。"""
    input_path = Path(input_arg).expanduser().resolve()
    if not input_path.is_file():
        raise HLSException(f"输入文件不存在：{input_path}")

    output_dir = resolve_output_dir(input_path, output_arg)

    if fmt == "hls":
        playlist = output_dir / "playlist.m3u8"
        segments_dir = output_dir / "ts"

        if playlist == input_path:
            raise HLSException(f"输出文件与输入文件相同：{playlist}")
        if segments_dir.exists() and not segments_dir.is_dir():
            raise HLSException(f"分片路径不是目录：{segments_dir}")
        if not overwrite and (playlist.exists() or segments_dir.exists()):
            raise HLSException(
                f"目标已存在：{output_dir}，使用 --overwrite 覆盖"
            )

        if overwrite:
            playlist.unlink(missing_ok=True)
            if segments_dir.exists():
                shutil.rmtree(segments_dir)
        segments_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            build_hls_command(input_path, output_dir, codec, segment_time),
            check=True,
        )
        return playlist

    output = output_dir / (input_path.stem + FORMAT_SUFFIX[fmt])
    if output == input_path:
        raise HLSException(f"输出文件与输入文件相同：{output}")
    if output.exists() and not overwrite:
        raise HLSException(
            f"输出文件已存在：{output}，使用 --overwrite 覆盖"
        )

    partial = output_dir / (
        input_path.stem + ".partial" + FORMAT_SUFFIX[fmt]
    )
    try:
        subprocess.run(
            build_file_command(input_path, fmt, codec, partial),
            check=True,
        )
        partial.replace(output)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return output
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_convert -v`
Expected: `Ran 13 tests ... OK`

- [ ] **Step 5: 提交**

```bash
git add src/m3u8_cli/convert.py tests/test_convert.py
git commit -m "实现转换执行与覆盖语义"
```

---

### Task 4: 接入 convert 子命令

**Files:**
- Modify: `src/m3u8_cli/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_cli.py` 的 `DownloadParserTests` 之后追加：

```python
class ConvertParserTests(unittest.TestCase):
    def test_convert_defaults(self):
        args = build_parser().parse_args(["convert", "movie.mp4"])
        self.assertEqual(args.command, "convert")
        self.assertEqual(args.input, "movie.mp4")
        self.assertEqual(args.format, "hls")
        self.assertEqual(args.codec, "h264")
        self.assertIsNone(args.output_dir)
        self.assertEqual(args.segment_time, 6)
        self.assertFalse(args.overwrite)

    def test_convert_choices(self):
        args = build_parser().parse_args([
            "convert", "movie.mp4",
            "-f", "webm", "-c", "hevc",
            "-o", "out", "--segment-time", "10", "--overwrite",
        ])
        self.assertEqual(args.format, "webm")
        self.assertEqual(args.codec, "hevc")
        self.assertEqual(args.output_dir, "out")
        self.assertEqual(args.segment_time, 10)
        self.assertTrue(args.overwrite)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_cli -v`
Expected: FAIL，`convert` 不是合法子命令（`SystemExit`）。

- [ ] **Step 3: 接入 convert**

在 `src/m3u8_cli/cli.py` 的导入区加入：

```python
from .convert import convert_video
```

在 `build_parser()` 中 `download.add_argument("--overwrite", ...)` 之后、`return parser` 之前加入：

```python
    convert = subparsers.add_parser(
        "convert",
        help="把本地视频转换为 HLS 或其他格式",
        description="用 FFmpeg 转换视频格式，默认输出 HLS",
    )
    convert.add_argument("input", help="输入视频文件")
    convert.add_argument(
        "-f", "--format", choices=("hls", "mp4", "mkv", "webm"),
        default="hls", help="输出格式（默认 hls）",
    )
    convert.add_argument(
        "-c", "--codec", choices=("h264", "hevc", "copy"),
        default="h264", help="编码方式（默认 h264）",
    )
    convert.add_argument(
        "-o", "--output-dir",
        help="输出目录；默认为输入文件名",
    )
    convert.add_argument(
        "--segment-time", type=int, default=6,
        help="HLS 分片时长（秒，默认 6）",
    )
    convert.add_argument(
        "--overwrite", action="store_true",
        help="覆盖已有产物",
    )
```

在 `run_download()` 之后加入：

```python
def run_convert(args):
    if not shutil.which("ffmpeg"):
        sys.exit("请先安装 ffmpeg")

    output = convert_video(
        args.input,
        args.format,
        args.codec,
        args.output_dir,
        args.segment_time,
        args.overwrite,
    )
    print(f"完成：{output}")
```

把 `main()` 替换为：

```python
def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "download":
        if args.jobs < 1 or args.timeout < 1 or args.retries < 0:
            parser.error("jobs/timeout 必须为正数，retries 不能为负数")
        handler = run_download
    else:
        if args.segment_time < 1:
            parser.error("segment-time 必须为正整数")
        handler = run_convert

    try:
        handler(args)
    except (HLSException, requests.RequestException,
            subprocess.CalledProcessError) as exc:
        sys.exit(f"\n错误：{exc}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest discover -s tests -v`
Expected: `Ran 15 tests ... OK`

再运行：

```bash
uv run m3u8-cli --help
uv run m3u8-cli convert --help
```

Expected: 顶层帮助列出 `download` 与 `convert`；convert 帮助包含全部参数。

- [ ] **Step 5: 提交**

```bash
git add src/m3u8_cli/cli.py tests/test_cli.py
git commit -m "接入 convert 子命令"
```

---

### Task 5: 更新文档

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: 更新 README 使用方式**

把 README 中所有下载示例的命令改为带 `download` 子命令：

```bash
uv run m3u8-cli download "<m3u8_url>"
uv run m3u8-cli download "https://example.com/playlist.m3u8"
uv run m3u8-cli download "https://example.com/playlist.m3u8" -o movie.mp4 -j 16
```

在「常用示例」之后新增「转换视频」小节：

````markdown
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
| `-f, --format` | `hls` | `hls`、`mp4`、`mkv`、`webm` |
| `-c, --codec` | `h264` | `h264`、`hevc`、`copy` |
| `-o, --output-dir` | 输入文件 stem | 输出目录 |
| `--segment-time` | `6` | HLS 分片秒数，其他格式忽略 |
| `--overwrite` | 关闭 | 覆盖已有产物 |

HLS 输出固定为 `playlist.m3u8` 加 `ts/000000.ts` 起的分片；WebM 会强制使用 VP9 + Opus；`-c copy` 由 FFmpeg 判断源编码是否兼容。
````

在代码结构块中加入：

```
├── convert.py       # 视频格式转换（FFmpeg）
```

并在「开发」小节加入测试命令：

```bash
# 单元测试
uv run python -m unittest discover -s tests -v
```

- [ ] **Step 2: 更新 CLAUDE.md**

- 仓库概述中加入 `convert` 子命令说明。
- 常用命令改为 `uv run m3u8-cli download ...`，并新增：

```bash
# 转换视频（默认 HLS）
uv run m3u8-cli convert movie.mp4 -o out
uv run m3u8-cli convert movie.mp4 -f mkv -c hevc -o out
```

- 模块划分中加入：`convert.py`：格式白名单、编码映射、输出路径派生、覆盖检查与 FFmpeg 调用。
- 处理流程前补充子命令分发说明：`cli.py` 使用 subparsers，`main()` 按 `download` / `convert` 分发。
- 「已知限制」中加入：`convert` 只接受本地文件；不探测源编码；HLS 转换没有整体原子性。
- 补充测试命令：`uv run python -m unittest discover -s tests -v`。

- [ ] **Step 3: 检查文档命令**

Run: `grep -n "m3u8-cli download\|m3u8-cli convert" README.md CLAUDE.md`
Expected: 下载示例均带 `download`，转换示例带 `convert`。

- [ ] **Step 4: 提交**

```bash
git add README.md CLAUDE.md
git commit -m "更新子命令与 convert 文档"
```

---

### Task 6: 端到端验证

**Files:**
- 无代码改动；发现问题时回到对应任务修复

- [ ] **Step 1: 生成测试视频**

```bash
mkdir -p /tmp/m3u8-cli-e2e
ffmpeg -hide_banner -y \
  -f lavfi -i testsrc2=size=320x240:rate=25 \
  -f lavfi -i sine=frequency=440 \
  -t 3 -c:v libx264 -c:a aac -shortest \
  /tmp/m3u8-cli-e2e/sample.mp4
```

Expected: 生成 `/tmp/m3u8-cli-e2e/sample.mp4`。

- [ ] **Step 2: 验证 HLS 默认输出**

```bash
cd /tmp/m3u8-cli-e2e
uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4
find sample -type f | sort
ffprobe -v error -show_entries format=format_name \
  -of default=nw=1 sample/playlist.m3u8
```

Expected: 存在 `sample/playlist.m3u8` 和 `sample/ts/000000.ts` 等分片；ffprobe 输出包含 `hls`。

- [ ] **Step 3: 验证 mp4/mkv/webm 与 -o**

```bash
cd /tmp/m3u8-cli-e2e
uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4 -f mp4 -o out
uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4 -f mkv -c hevc -o out
uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4 -f webm -o out
find out -type f | sort
```

Expected: 生成 `out/sample.mp4`、`out/sample.mkv`、`out/sample.webm`，无 `.partial` 残留。

- [ ] **Step 4: 验证覆盖与 copy**

```bash
cd /tmp/m3u8-cli-e2e
uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4 -f mp4 -o out
# 预期：报错“输出文件已存在”

uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4 -f mp4 -o out --overwrite
# 预期：成功

uv run --project /home/zcq100/Documents/projects/m3u8_download \
  m3u8-cli convert sample.mp4 -f hls -c copy -o copy-out
# 预期：成功（源为 H.264/AAC）
```

- [ ] **Step 5: 最终检查**

```bash
cd /home/zcq100/Documents/projects/m3u8_download
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src
```

Expected: 全部测试通过，compileall 无输出。

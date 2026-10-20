# HLS 保留下载与 serve API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为下载器增加 `download -f hls`（保留播放列表、分片与本地密钥，不合并），并新增 `serve` 子命令，通过局域网 RESTful API 异步接收下载任务、查询任务列表和取消任务。

**Architecture:** 先把现有 `cli.run_download()` 的下载流程抽成 `download.py` 中的 `download_hls()`，CLI 与 serve 共用；`-f hls` 通过新的 `hls_export.py` 把缓存分片导出为扁平本地 HLS 目录；`serve.py` 用标准库 `ThreadingHTTPServer` 提供 HTTP 层，内部 `TaskManager` 负责 FIFO 调度、并发上限、任务持久化与协作式取消。

**Tech Stack:** Python 3.12、标准库（`http.server`、`threading`、`json`、`dataclasses`）、`requests`、`pycryptodome`、FFmpeg（仅 `-f mp4`）、标准库 `unittest`。

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `src/m3u8_cli/playlist.py` | 增加 `EXTINF` 时长与原始媒体序列号 |
| `src/m3u8_cli/errors.py` | 新增 `DownloadCancelled` |
| `src/m3u8_cli/downloader.py` | 支持不解密的原始分片下载 |
| `src/m3u8_cli/paths.py` | 拆出无副作用的 `derive_task_paths()` |
| `src/m3u8_cli/hls_export.py` | 生成本地播放列表、复制分片与密钥 |
| `src/m3u8_cli/download.py` | 可复用下载核心，支持 `mp4` / `hls` |
| `src/m3u8_cli/serve.py` | 任务管理器与 REST API |
| `src/m3u8_cli/cli.py` | `download -f`、`serve` 子命令 |
| `tests/test_playlist.py` | 解析器测试 |
| `tests/test_downloader.py` | 原始模式与密钥校验测试 |
| `tests/test_hls_export.py` | 播放列表生成与导出测试 |
| `tests/test_download.py` | 下载核心测试 |
| `tests/test_serve.py` | TaskManager 测试 |
| `tests/test_serve_api.py` | HTTP API 集成测试 |
| `tests/test_cli.py` | 参数解析测试更新 |
| `README.md` / `CLAUDE.md` / `.gitignore` | 文档与忽略规则 |

---

## Task 1: 解析 EXTINF 时长与媒体序列号

**Files:**
- Modify: `src/m3u8_cli/playlist.py:65-150`
- Test: `tests/test_playlist.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
import unittest
from unittest import mock

from m3u8_cli.playlist import load_playlist

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-TARGETDURATION:7
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x0123456789abcdef0123456789abcdef
#EXTINF:4.0,
seg0.ts
#EXTINF:6.5,
seg1.ts
#EXT-X-ENDLIST
"""


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PlaylistTests(unittest.TestCase):
    def test_captures_durations_and_media_sequence(self):
        with mock.patch(
            "m3u8_cli.playlist.requests.get",
            return_value=FakeResponse(PLAYLIST),
        ):
            playlist = load_playlist(
                "https://example.com/a.m3u8", {}, 30
            )

        self.assertEqual(playlist["media_sequence"], 7)
        segments = playlist["segments"]
        self.assertEqual([s["duration"] for s in segments], [4.0, 6.5])
        self.assertEqual([s["sequence"] for s in segments], [7, 8])
        self.assertEqual(
            segments[0]["key"]["uri"],
            "https://example.com/key.bin",
        )

    def test_master_playlist_selection_keeps_durations(self):
        master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000
high.m3u8
"""
        media = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:5.0,
s.ts
#EXT-X-ENDLIST
"""
        responses = [FakeResponse(master), FakeResponse(media)]
        with mock.patch(
            "m3u8_cli.playlist.requests.get",
            side_effect=responses,
        ):
            playlist = load_playlist(
                "https://example.com/master.m3u8", {}, 30
            )

        self.assertEqual(playlist["segments"][0]["duration"], 5.0)
        self.assertEqual(
            playlist["segments"][0]["url"],
            "https://example.com/s.ts",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_playlist -v`
Expected: FAIL，`KeyError: 'media_sequence'` 或 `KeyError: 'duration'`

- [ ] **Step 3: 修改 `playlist.py`**

把媒体播放列表解析段改为下面的形式（保留原有 master playlist 逻辑和校验）：

```python
    media_sequence = 0
    key = None
    current_map = None
    segments = []
    maps = []
    pending_map = None
    pending_duration = 0.0

    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except ValueError as exc:
                raise HLSException("无效的 MEDIA-SEQUENCE") from exc

        elif line.startswith("#EXTINF:"):
            try:
                pending_duration = float(
                    line.split(":", 1)[1].split(",", 1)[0]
                )
            except ValueError as exc:
                raise HLSException("无效的 EXTINF") from exc

        elif line.startswith("#EXT-X-KEY:"):
            attrs = parse_attrs(line)
            method = attrs.get("METHOD", "")

            if method == "NONE":
                key = None
            elif method == "AES-128":
                keyformat = attrs.get("KEYFORMAT", "identity")
                if keyformat != "identity":
                    raise HLSException(
                        f"不支持的 KEYFORMAT：{keyformat}"
                    )
                if not attrs.get("URI"):
                    raise HLSException("AES-128 标签缺少密钥 URI")
                key = {
                    "method": method,
                    "uri": urljoin(url, attrs["URI"]),
                    "iv": attrs.get("IV"),
                }
            else:
                raise HLSException(
                    f"不支持的加密方式：{method}；不处理 DRM"
                )

        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attrs(line)
            if not attrs.get("URI"):
                raise HLSException("EXT-X-MAP 缺少 URI")
            if "BYTERANGE" in attrs:
                raise HLSException("暂不支持初始化片段 BYTERANGE")
            pending_map = urljoin(url, attrs["URI"])

        elif line.startswith("#EXT-X-BYTERANGE:"):
            raise HLSException(
                "暂不支持 EXT-X-BYTERANGE，请使用完整分片 URL"
            )

        elif not line.startswith("#"):
            segment_key = dict(key) if key else None
            segment_map = pending_map or current_map

            if pending_map:
                current_map = pending_map
                pending_map = None

            segments.append({
                "url": urljoin(url, line),
                "sequence": media_sequence + len(segments),
                "duration": pending_duration,
                "key": segment_key,
                "map": segment_map,
            })
            pending_duration = 0.0
```

并把返回字典改为：

```python
    return {
        "playlist_url": url,
        "segments": segments,
        "maps": maps,
        "media_sequence": media_sequence,
        "fingerprint": hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest(),
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_playlist -v`
Expected: PASS（2 个测试）

- [ ] **Step 5: 运行现有测试**

Run: `uv run python -m unittest discover -s tests -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/m3u8_cli/playlist.py tests/test_playlist.py
git commit -m "解析 HLS 分片时长与媒体序列号"
```

---

## Task 2: 支持原始密文分片下载

**Files:**
- Modify: `src/m3u8_cli/errors.py:1-2`
- Modify: `src/m3u8_cli/downloader.py:79-102`
- Test: `tests/test_downloader.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from m3u8_cli.downloader import Downloader
from m3u8_cli.errors import HLSException


class DownloaderRawTests(unittest.TestCase):
    def test_raw_mode_skips_decryption(self):
        downloader = Downloader({}, 30, 0)
        ciphertext = b"\x00" * 32

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "segment.bin"
            with mock.patch.object(
                downloader, "get_bytes", return_value=ciphertext
            ), mock.patch.object(
                downloader,
                "decrypt",
                side_effect=AssertionError("不应解密"),
            ):
                result = downloader.download(
                    "https://example.com/s.ts",
                    target,
                    decrypt=False,
                )

            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), ciphertext)

    def test_key_length_validated(self):
        downloader = Downloader({}, 30, 0)
        with mock.patch.object(
            downloader, "get_bytes", return_value=b"short"
        ):
            with self.assertRaises(HLSException):
                downloader.get_key("https://example.com/key.bin")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_downloader -v`
Expected: FAIL，`TypeError: download() got an unexpected keyword argument 'decrypt'`

- [ ] **Step 3: 修改 `errors.py`**

```python
class HLSException(RuntimeError):
    pass


class DownloadCancelled(HLSException):
    """任务被协作式取消。"""
```

- [ ] **Step 4: 修改 `downloader.py`**

```python
    def download(
        self, url, path, key_info=None, sequence=0, decrypt=True
    ):
        if path.exists() and path.stat().st_size > 0:
            return path

        last_error = None

        for attempt in range(self.retries + 1):
            try:
                data = self.get_bytes(url)
                if decrypt:
                    data = self.decrypt(data, key_info, sequence)

                temp = path.with_suffix(path.suffix + ".part")
                temp.write_bytes(data)
                temp.replace(path)
                return path

            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 10))

        raise HLSException(
            f"下载失败：{url}\n最后错误：{last_error}"
        )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_downloader -v`
Expected: PASS（2 个测试）

- [ ] **Step 6: 提交**

```bash
git add src/m3u8_cli/errors.py src/m3u8_cli/downloader.py tests/test_downloader.py
git commit -m "支持原始密文分片下载"
```

---

## Task 3: 本地 HLS 导出模块

**Files:**
- Create: `src/m3u8_cli/hls_export.py`
- Test: `tests/test_hls_export.py`

- [ ] **Step 1: 写失败测试**

```python
import tempfile
import unittest
from pathlib import Path

from m3u8_cli.hls_export import build_playlist, export_hls


def media_playlist(keys=None, maps=None, durations=(4.0, 6.0)):
    segments = []
    for i, duration in enumerate(durations):
        segments.append({
            "url": f"https://example.com/{i}.ts",
            "sequence": i,
            "duration": duration,
            "key": keys[i] if keys else None,
            "map": None,
        })
    return {
        "playlist_url": "https://example.com/a.m3u8",
        "fingerprint": "abc",
        "media_sequence": 10,
        "maps": maps or [],
        "segments": segments,
    }


class BuildPlaylistTests(unittest.TestCase):
    def test_plain_ts_playlist(self):
        text = build_playlist(media_playlist(), {})

        self.assertIn("#EXT-X-VERSION:3", text)
        self.assertIn("#EXT-X-TARGETDURATION:6", text)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:10", text)
        self.assertIn("#EXTINF:4.000,\n000000.ts", text)
        self.assertIn("#EXTINF:6.000,\n000001.ts", text)
        self.assertNotIn("#EXT-X-KEY", text)
        self.assertTrue(text.endswith("#EXT-X-ENDLIST\n"))

    def test_single_key_is_rewritten(self):
        key = {
            "method": "AES-128",
            "uri": "https://example.com/key.bin",
            "iv": None,
        }
        text = build_playlist(
            media_playlist(keys=[key, key]),
            {key["uri"]: "key.bin"},
        )

        self.assertEqual(text.count("#EXT-X-KEY:"), 1)
        self.assertIn('URI="key.bin"', text)

    def test_multiple_keys_and_none_transition(self):
        key1 = {
            "method": "AES-128",
            "uri": "https://example.com/1",
            "iv": None,
        }
        key2 = {
            "method": "AES-128",
            "uri": "https://example.com/2",
            "iv": None,
        }
        text = build_playlist(
            media_playlist(keys=[key1, key2]),
            {
                key1["uri"]: "key_000.bin",
                key2["uri"]: "key_001.bin",
            },
        )

        self.assertIn('URI="key_000.bin"', text)
        self.assertIn('URI="key_001.bin"', text)

        text = build_playlist(
            media_playlist(keys=[key1, None]),
            {key1["uri"]: "key.bin"},
        )
        self.assertIn("#EXT-X-KEY:METHOD=NONE", text)

    def test_fmp4_playlist(self):
        playlist = media_playlist(
            maps=["https://example.com/init.mp4"]
        )
        text = build_playlist(playlist, {})

        self.assertIn("#EXT-X-VERSION:6", text)
        self.assertIn('#EXT-X-MAP:URI="init.mp4"', text)
        self.assertIn("000000.m4s", text)


class ExportTests(unittest.TestCase):
    def test_copies_segments_and_playlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            output = root / "out"
            cache.mkdir()
            (cache / "segment_000000.bin").write_bytes(b"a")
            (cache / "segment_000001.bin").write_bytes(b"b")

            result = export_hls(
                media_playlist(), cache, output, {}, {}, False
            )

            self.assertEqual(result, output)
            self.assertEqual((output / "000000.ts").read_bytes(), b"a")
            self.assertEqual((output / "000001.ts").read_bytes(), b"b")
            self.assertIn(
                "#EXTINF:4.000,",
                (output / "playlist.m3u8").read_text("utf-8"),
            )

    def test_overwrite_only_clears_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            output = root / "out"
            cache.mkdir()
            output.mkdir()
            (cache / "segment_000000.bin").write_bytes(b"a")
            (output / "000000.ts").write_bytes(b"old")
            (output / "notes.txt").write_text("keep")

            export_hls(
                media_playlist(durations=(4.0,)),
                cache,
                output,
                {},
                {},
                True,
            )

            self.assertEqual(
                (output / "000000.ts").read_bytes(), b"a"
            )
            self.assertEqual(
                (output / "notes.txt").read_text("utf-8"), "keep"
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_hls_export -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'm3u8_cli.hls_export'`

- [ ] **Step 3: 创建 `hls_export.py`**

```python
"""把已下载的 HLS 资源导出为本地目录。"""

import math
import re
import shutil
from pathlib import Path

from .errors import HLSException

SEGMENT_RE = re.compile(r"\d{6}\.(?:ts|m4s)")
KEY_RE = re.compile(r"key(?:_\d{3})?\.bin")


def segment_suffix(playlist):
    """fMP4 用 .m4s，其余沿用 .ts。"""
    return ".m4s" if playlist["maps"] else ".ts"


def build_playlist(playlist, key_names):
    """生成引用本地文件的可播放媒体播放列表。"""
    suffix = segment_suffix(playlist)
    segments = playlist["segments"]
    durations = [float(s.get("duration") or 0) for s in segments]
    target = max(1, math.ceil(max(durations) if durations else 0))

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6" if playlist["maps"] else "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-MEDIA-SEQUENCE:{playlist['media_sequence']}",
    ]
    if playlist["maps"]:
        lines.append('#EXT-X-MAP:URI="init.mp4"')

    previous_key = None
    for i, segment in enumerate(segments):
        key = segment.get("key")
        if key is None and previous_key is not None:
            lines.append("#EXT-X-KEY:METHOD=NONE")
        elif key is not None and key != previous_key:
            attrs = [
                f'METHOD={key["method"]}',
                f'URI="{key_names[key["uri"]]}"',
            ]
            if key.get("iv"):
                attrs.append(f'IV={key["iv"]}')
            lines.append("#EXT-X-KEY:" + ",".join(attrs))
        previous_key = key
        lines.append(f"#EXTINF:{durations[i]:.3f},")
        lines.append(f"{i:06d}{suffix}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _clear_output(output_dir):
    """只删除本工具生成的播放列表、分片、初始化片段与密钥。"""
    for entry in output_dir.iterdir():
        if not (entry.is_file() or entry.is_symlink()):
            continue
        if (
            entry.name == "playlist.m3u8"
            or entry.name == "init.mp4"
            or SEGMENT_RE.fullmatch(entry.name)
            or KEY_RE.fullmatch(entry.name)
        ):
            entry.unlink()


def export_hls(playlist, cache, output_dir, key_data, key_names,
               overwrite):
    """把缓存分片、初始化片段与密钥导出到本地 HLS 目录。"""
    playlist_path = output_dir / "playlist.m3u8"
    if not overwrite and playlist_path.exists():
        raise HLSException(
            f"输出文件已存在：{playlist_path}，使用 --overwrite 覆盖"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    if overwrite:
        _clear_output(output_dir)

    suffix = segment_suffix(playlist)
    for i in range(len(playlist["segments"])):
        source = cache / f"segment_{i:06d}.bin"
        shutil.copyfile(source, output_dir / f"{i:06d}{suffix}")

    if playlist["maps"]:
        shutil.copyfile(
            cache / "map_0000.bin", output_dir / "init.mp4"
        )

    for uri, name in key_names.items():
        (output_dir / name).write_bytes(key_data[uri])

    part = output_dir / "playlist.m3u8.part"
    part.write_text(build_playlist(playlist, key_names), "utf-8")
    part.replace(playlist_path)
    return output_dir
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_hls_export -v`
Expected: PASS（6 个测试）

- [ ] **Step 5: 提交**

```bash
git add src/m3u8_cli/hls_export.py tests/test_hls_export.py
git commit -m "添加本地 HLS 导出模块"
```

---

## Task 4: 抽离下载核心并接入 `-f`

**Files:**
- Modify: `src/m3u8_cli/paths.py:8-22`
- Create: `src/m3u8_cli/download.py`
- Modify: `src/m3u8_cli/cli.py:27-46,81-175`
- Test: `tests/test_download.py`（新建）
- Test: `tests/test_cli.py:9-22`

- [ ] **Step 1: 写失败测试（下载核心）**

```python
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from m3u8_cli.download import DownloadOptions, download_hls
from m3u8_cli.errors import DownloadCancelled


class FakeDownloader:
    decrypt_flags = []

    def __init__(self, headers, timeout, retries):
        pass

    def download(self, url, path, key_info=None, sequence=0,
                 decrypt=True):
        FakeDownloader.decrypt_flags.append(decrypt)
        path.write_bytes(b"data")
        return path

    def get_key(self, uri):
        return b"k" * 16


def make_playlist(segments=2, maps=None):
    return {
        "playlist_url": "https://example.com/a.m3u8",
        "fingerprint": "abc",
        "media_sequence": 0,
        "maps": maps or [],
        "segments": [
            {
                "url": f"https://example.com/{i}.ts",
                "sequence": i,
                "duration": 4.0,
                "key": None,
                "map": None,
            }
            for i in range(segments)
        ],
    }


class DownloadCoreTests(unittest.TestCase):
    def setUp(self):
        FakeDownloader.decrypt_flags = []

    def test_mp4_flow_reports_progress_and_muxes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "movie" / "movie.mp4"
            progress = []

            with mock.patch(
                "m3u8_cli.download.load_playlist",
                return_value=make_playlist(),
            ), mock.patch(
                "m3u8_cli.download.Downloader", FakeDownloader
            ), mock.patch(
                "m3u8_cli.download.combine_segments",
                return_value=Path(tmp) / "combined.ts",
            ), mock.patch(
                "m3u8_cli.download.mux"
            ) as mux:
                result = download_hls(
                    DownloadOptions(
                        url="https://example.com/a.m3u8",
                        output=output,
                    ),
                    on_progress=lambda done, total: progress.append(
                        (done, total)
                    ),
                )

            self.assertEqual(result, output)
            self.assertEqual(progress, [(1, 2), (2, 2)])
            mux.assert_called_once()
            self.assertTrue(all(FakeDownloader.decrypt_flags))

    def test_cancel_before_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel = threading.Event()
            cancel.set()

            with self.assertRaises(DownloadCancelled):
                download_hls(
                    DownloadOptions(
                        url="https://example.com/a.m3u8",
                        output=Path(tmp) / "movie" / "movie.mp4",
                    ),
                    cancel=cancel,
                )

    def test_mode_change_clears_cached_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "movie" / "movie.mp4"
            cache = output.parent / "cache"
            cache.mkdir(parents=True)
            (cache / "segment_000000.bin").write_bytes(b"old")
            (cache / "state.json").write_text(
                json.dumps({"fingerprint": "abc", "mode": "hls"}),
                "utf-8",
            )

            with mock.patch(
                "m3u8_cli.download.load_playlist",
                return_value=make_playlist(segments=0),
            ), mock.patch(
                "m3u8_cli.download.Downloader", FakeDownloader
            ), mock.patch(
                "m3u8_cli.download.combine_segments",
                return_value=Path(tmp) / "combined.ts",
            ), mock.patch(
                "m3u8_cli.download.mux"
            ):
                download_hls(
                    DownloadOptions(
                        url="https://example.com/a.m3u8",
                        output=output,
                    )
                )

            state = json.loads(
                (cache / "state.json").read_text("utf-8")
            )
            self.assertEqual(state["mode"], "mp4")
            self.assertFalse((cache / "segment_000000.bin").exists())

    def test_hls_flow_keeps_raw_segments_and_keys(self):
        key = {
            "method": "AES-128",
            "uri": "https://example.com/key.bin",
            "iv": None,
        }
        playlist = make_playlist(segments=1)
        playlist["segments"][0]["key"] = key

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "movie"

            with mock.patch(
                "m3u8_cli.download.load_playlist",
                return_value=playlist,
            ), mock.patch(
                "m3u8_cli.download.Downloader", FakeDownloader
            ):
                result = download_hls(
                    DownloadOptions(
                        url="https://example.com/a.m3u8",
                        output=output,
                        format="hls",
                    )
                )

            self.assertEqual(result, output)
            self.assertEqual(
                (output / "000000.ts").read_bytes(), b"data"
            )
            self.assertEqual(
                (output / "key.bin").read_bytes(), b"k" * 16
            )
            self.assertFalse(any(FakeDownloader.decrypt_flags))
            text = (output / "playlist.m3u8").read_text("utf-8")
            self.assertIn('URI="key.bin"', text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_download -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'm3u8_cli.download'`

- [ ] **Step 3: 修改 `paths.py`**

```python
def derive_task_paths(output_arg):
    """返回最终文件路径和缓存目录，不创建任何目录。"""
    output = Path(output_arg).expanduser().resolve()

    # -o output/name.mp4 时直接复用 output/，避免嵌套成 output/output/。
    if output.parent.name == output.stem:
        task_dir = output.parent
    else:
        task_dir = output.parent / output.stem

    return output, task_dir / "cache"


def resolve_task_paths(output_arg):
    """返回任务目录内的最终文件路径和缓存目录，并创建任务目录。"""
    output, cache = derive_task_paths(output_arg)
    task_dir = cache.parent

    if task_dir.exists() and not task_dir.is_dir():
        raise HLSException(f"任务目录路径不是目录：{task_dir}")

    task_dir.mkdir(parents=True, exist_ok=True)
    return output, cache
```

- [ ] **Step 4: 创建 `download.py`**

```python
"""可复用的 HLS 下载流程。"""

import concurrent.futures
import json
from dataclasses import dataclass, field
from pathlib import Path

from .downloader import Downloader
from .errors import DownloadCancelled, HLSException
from .hls_export import export_hls
from .merge import combine_segments, mux
from .paths import resolve_task_paths
from .playlist import load_playlist


@dataclass
class DownloadOptions:
    url: str
    output: Path
    format: str = "mp4"
    jobs: int = 8
    retries: int = 3
    timeout: int = 30
    headers: dict = field(default_factory=dict)
    overwrite: bool = False


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("下载已取消")


def _resolve_paths(options):
    if options.format == "hls":
        task_dir = Path(options.output).expanduser().resolve()
        if task_dir.exists() and not task_dir.is_dir():
            raise HLSException(f"任务目录不是目录：{task_dir}")
        return task_dir / "playlist.m3u8", task_dir / "cache"
    return resolve_task_paths(options.output)


def _load_state(state_file):
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def download_hls(options, on_progress=None, cancel=None):
    """下载 HLS 并输出；mp4 返回最终文件，hls 返回任务目录。"""
    if options.format not in ("mp4", "hls"):
        raise HLSException(f"不支持的输出格式：{options.format}")

    _check_cancel(cancel)
    final, cache = _resolve_paths(options)
    task_dir = cache.parent

    if final.exists() and not options.overwrite:
        raise HLSException(
            f"输出文件已存在：{final}，使用 --overwrite 覆盖"
        )

    cache.mkdir(parents=True, exist_ok=True)

    print("读取 m3u8...")
    playlist = load_playlist(options.url, options.headers, options.timeout)
    segments = playlist["segments"]
    fingerprint = playlist["fingerprint"]

    state_file = cache / "state.json"
    previous = _load_state(state_file)
    if previous and (
        previous.get("fingerprint") != fingerprint
        or previous.get("mode") != options.format
    ):
        print("播放列表或输出模式已变化，清除旧分片缓存")
        for p in cache.glob("segment_*.bin"):
            p.unlink(missing_ok=True)
        for p in cache.glob("map_*.bin"):
            p.unlink(missing_ok=True)

    state_file.write_text(
        json.dumps(
            {"fingerprint": fingerprint, "mode": options.format},
            indent=2,
        ),
        encoding="utf-8",
    )

    downloader = Downloader(
        options.headers, options.timeout, options.retries
    )

    key_data = {}
    key_names = {}
    if options.format == "hls":
        key_uris = list(dict.fromkeys(
            s["key"]["uri"] for s in segments if s.get("key")
        ))
        for uri in key_uris:
            _check_cancel(cancel)
            key_data[uri] = downloader.get_key(uri)
        for index, uri in enumerate(key_uris):
            key_names[uri] = (
                "key.bin" if len(key_uris) == 1
                else f"key_{index:03d}.bin"
            )

    map_paths = {}
    for i, map_url in enumerate(playlist["maps"]):
        _check_cancel(cancel)
        path = cache / f"map_{i:04d}.bin"
        map_paths[map_url] = downloader.download(
            map_url, path, decrypt=False
        )
        print(f"初始化片段已准备：{i + 1}/{len(playlist['maps'])}")

    jobs = [
        (i, segment, cache / f"segment_{i:06d}.bin")
        for i, segment in enumerate(segments)
    ]
    print(f"共 {len(jobs)} 个分片，并发数 {options.jobs}")

    completed = 0
    decrypt = options.format == "mp4"

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=options.jobs
    ) as pool:
        pending = {}
        iterator = iter(jobs)

        def submit_next():
            try:
                i, segment, path = next(iterator)
            except StopIteration:
                return False
            future = pool.submit(
                downloader.download,
                segment["url"],
                path,
                segment["key"],
                segment["sequence"],
                decrypt,
            )
            pending[future] = (i, segment, path)
            return True

        try:
            _check_cancel(cancel)
            for _ in range(options.jobs * 2):
                if not submit_next():
                    break

            while pending:
                done, _ = concurrent.futures.wait(
                    pending,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    pending.pop(future)
                    future.result()
                    completed += 1
                    if on_progress is not None:
                        on_progress(completed, len(jobs))
                _check_cancel(cancel)
                for _ in range(len(done)):
                    if not submit_next():
                        break
        except BaseException:
            for future in pending:
                future.cancel()
            raise

    if options.format == "hls":
        _check_cancel(cancel)
        return export_hls(
            playlist,
            cache,
            task_dir,
            key_data,
            key_names,
            options.overwrite,
        )

    print("\n所有分片下载完成，开始本地合并")
    combined = combine_segments(
        segments, playlist["maps"], map_paths, cache
    )
    mux(combined, cache, final)
    return final
```

- [ ] **Step 5: 修改 `cli.py` 的下载参数与流程**

在 `build_parser()` 的 download 子命令中增加：

```python
    download.add_argument(
        "-f", "--format", choices=("mp4", "hls"), default="mp4",
        help="输出格式：mp4（合并封装，默认）或 hls（保留分片，不合并）",
    )
```

把 `-o` 的默认值去掉，改为在流程中按格式决定：

```python
    download.add_argument(
        "-o", "--output",
        help="mp4: 最终文件名；hls: 任务目录（默认 output.mp4 / output）",
    )
```

用下面的实现替换 `run_download()`：

```python
def run_download(args):
    if args.format == "mp4" and not shutil.which("ffmpeg"):
        sys.exit("请先安装 ffmpeg")

    output_arg = args.output or (
        "output" if args.format == "hls" else "output.mp4"
    )

    headers = {"User-Agent": args.user_agent}
    if args.referer:
        headers["Referer"] = args.referer
    if args.cookie:
        headers["Cookie"] = args.cookie

    def on_progress(completed, total):
        print(f"\r下载进度：{completed}/{total}", end="", flush=True)

    output = download_hls(
        DownloadOptions(
            url=args.url,
            output=Path(output_arg),
            format=args.format,
            jobs=args.jobs,
            retries=args.retries,
            timeout=args.timeout,
            headers=headers,
            overwrite=args.overwrite,
        ),
        on_progress=on_progress,
    )
    print()
    print(f"完成：{output}")
```

在文件顶部增加导入：

```python
from pathlib import Path

from .download import DownloadOptions, download_hls
```

并删除不再使用的导入（`concurrent.futures`、`json`、`Downloader`、`combine_segments`、`mux`、`resolve_task_paths`、`load_playlist`）。

- [ ] **Step 6: 更新 CLI 测试**

把 `tests/test_cli.py` 的 `test_download_subcommand` 改为：

```python
    def test_download_subcommand(self):
        args = build_parser().parse_args(
            ["download", "https://example.com/a.m3u8"]
        )
        self.assertEqual(args.command, "download")
        self.assertEqual(args.url, "https://example.com/a.m3u8")
        self.assertIsNone(args.output)
        self.assertEqual(args.format, "mp4")
        self.assertEqual(args.jobs, 8)

    def test_download_hls_format(self):
        args = build_parser().parse_args(
            ["download", "https://example.com/a.m3u8", "-f", "hls"]
        )
        self.assertEqual(args.format, "hls")
```

- [ ] **Step 7: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_download tests.test_cli -v`
Expected: PASS

- [ ] **Step 8: 运行全部测试**

Run: `uv run python -m unittest discover -s tests -v`
Expected: 全部 PASS

- [ ] **Step 9: 提交**

```bash
git add src/m3u8_cli/paths.py src/m3u8_cli/download.py src/m3u8_cli/cli.py tests/test_download.py tests/test_cli.py
git commit -m "抽离下载核心并支持 -f hls"
```

---

## Task 5: serve 任务管理器

**Files:**
- Create: `src/m3u8_cli/serve.py`
- Test: `tests/test_serve.py`

- [ ] **Step 1: 写失败测试**

```python
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from m3u8_cli.errors import DownloadCancelled
from m3u8_cli.serve import TaskConflict, TaskManager, TaskNotFound


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def submit(manager, output, fmt="hls", overwrite=False):
    return manager.submit(
        url="https://example.com/a.m3u8",
        fmt=fmt,
        output=output,
        jobs=1,
        retries=3,
        timeout=30,
        overwrite=overwrite,
        headers={},
    )


class TaskManagerTests(unittest.TestCase):
    def test_success_updates_state_and_progress(self):
        def runner(options, on_progress=None, cancel=None):
            on_progress(1, 2)
            on_progress(2, 2)
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            record = submit(manager, "movie")

            self.assertTrue(wait_for(
                lambda: manager.get(record["id"])["state"]
                == "succeeded"
            ))
            task = manager.get(record["id"])
            self.assertEqual(task["completed"], 2)
            self.assertEqual(task["total"], 2)
            self.assertEqual(
                task["final_path"], str(Path(tmp) / "movie")
            )
            manager.close()

    def test_queue_respects_max_tasks(self):
        gate = threading.Event()

        def runner(options, on_progress=None, cancel=None):
            gate.wait(2)
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            first = submit(manager, "one")
            second = submit(manager, "two")

            self.assertTrue(wait_for(
                lambda: manager.get(first["id"])["state"] == "running"
            ))
            self.assertEqual(
                manager.get(second["id"])["state"], "queued"
            )

            gate.set()
            self.assertTrue(wait_for(
                lambda: manager.get(second["id"])["state"]
                == "succeeded"
            ))
            manager.close()

    def test_cancel_queued_task(self):
        def runner(options, on_progress=None, cancel=None):
            if wait_for(cancel.is_set, timeout=2):
                raise DownloadCancelled("cancelled")
            raise AssertionError("未被取消")

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            first = submit(manager, "one")
            second = submit(manager, "two")
            self.assertTrue(wait_for(
                lambda: manager.get(first["id"])["state"] == "running"
            ))

            manager.cancel(second["id"])
            self.assertEqual(
                manager.get(second["id"])["state"], "cancelled"
            )
            manager.cancel(first["id"])
            self.assertTrue(wait_for(
                lambda: manager.get(first["id"])["state"]
                == "cancelled"
            ))
            manager.close()

    def test_cancel_running_task(self):
        started = threading.Event()

        def runner(options, on_progress=None, cancel=None):
            started.set()
            if wait_for(cancel.is_set, timeout=2):
                raise DownloadCancelled("cancelled")
            raise AssertionError("未被取消")

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            record = submit(manager, "movie")
            self.assertTrue(started.wait(1))

            manager.cancel(record["id"])
            self.assertTrue(wait_for(
                lambda: manager.get(record["id"])["state"]
                == "cancelled"
            ))
            manager.close()

    def test_duplicate_output_conflicts(self):
        gate = threading.Event()

        def runner(options, on_progress=None, cancel=None):
            gate.wait(2)
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            submit(manager, "movie")
            with self.assertRaises(TaskConflict):
                submit(manager, "movie")
            gate.set()
            manager.close()

    def test_existing_target_conflicts_without_overwrite(self):
        def runner(options, on_progress=None, cancel=None):
            return options.output

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "movie"
            target.mkdir()
            (target / "playlist.m3u8").write_text("old")

            manager = TaskManager(tmp, max_tasks=1, runner=runner)
            with self.assertRaises(TaskConflict):
                submit(manager, "movie")
            manager.close()

    def test_reload_marks_unfinished_tasks_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({
                "version": 1,
                "tasks": [{
                    "id": "abc",
                    "url": "https://example.com/a.m3u8",
                    "format": "mp4",
                    "output": "output.mp4",
                    "state": "running",
                }],
            }))

            manager = TaskManager(
                tmp,
                max_tasks=1,
                state_file=state,
                runner=lambda *a, **k: None,
            )
            self.assertEqual(
                manager.get("abc")["state"], "interrupted"
            )
            manager.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_serve -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'm3u8_cli.serve'`

- [ ] **Step 3: 创建 `serve.py`（本任务只实现任务管理器部分）**

```python
"""RESTful 下载服务。"""

import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .download import DownloadOptions, download_hls
from .errors import DownloadCancelled, HLSException
from .paths import derive_task_paths

KEEP_TERMINAL = 200
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted"}


class RequestError(ValueError):
    pass


class TaskConflict(RuntimeError):
    pass


class TaskNotFound(KeyError):
    pass


def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class TaskRecord:
    id: str
    url: str
    format: str
    output: str
    state: str = "queued"
    final_path: str | None = None
    created_at: str = field(default_factory=utcnow)
    started_at: str | None = None
    finished_at: str | None = None
    completed: int = 0
    total: int = 0
    error: str | None = None

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.url,
            "format": self.format,
            "output": self.output,
            "state": self.state,
            "final_path": self.final_path,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "completed": self.completed,
            "total": self.total,
            "error": self.error,
        }


class TaskManager:
    def __init__(self, output_dir, max_tasks=3, default_jobs=8,
                 state_file=None, runner=None):
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_tasks = max_tasks
        self.default_jobs = default_jobs
        self.state_file = (
            Path(state_file).expanduser()
            if state_file is not None
            else self.output_dir / ".m3u8-serve-state.json"
        )
        self.runner = runner or download_hls

        self.lock = threading.RLock()
        self.tasks = {}
        self.order = []
        self.queue = []
        self.running = set()
        self._params = {}
        self._cancels = {}
        self._dirty = False
        self._stop = threading.Event()

        self._load()
        self._flusher = threading.Thread(
            target=self._flush_loop, daemon=True
        )
        self._flusher.start()

    def _task_paths(self, fmt, output):
        if fmt == "mp4":
            final, cache = derive_task_paths(self.output_dir / output)
            return final, cache.parent
        task_dir = (self.output_dir / output).resolve()
        return task_dir / "playlist.m3u8", task_dir

    def submit(self, *, url, fmt, output, jobs, retries, timeout,
               overwrite, headers):
        if (
            not output
            or Path(output).name != output
            or output in (".", "..")
        ):
            raise RequestError("output 必须是纯文件名")

        final, task_dir = self._task_paths(fmt, output)
        key = str(task_dir)

        with self.lock:
            for task in self.tasks.values():
                if task.state in ("queued", "running"):
                    _, active_dir = self._task_paths(
                        task.format, task.output
                    )
                    if str(active_dir) == key:
                        raise TaskConflict(
                            f"输出正在被任务 {task.id} 使用"
                        )
            if not overwrite and final.exists():
                raise TaskConflict(
                    f"目标已存在：{final}，使用 overwrite 覆盖"
                )

            record = TaskRecord(
                id=uuid.uuid4().hex,
                url=url,
                format=fmt,
                output=output,
            )
            self.tasks[record.id] = record
            self.order.append(record.id)
            self.queue.append(record.id)
            self._params[record.id] = {
                "url": url,
                "format": fmt,
                "output": self.output_dir / output,
                "jobs": jobs,
                "retries": retries,
                "timeout": timeout,
                "headers": dict(headers),
                "overwrite": overwrite,
            }
            self._cancels[record.id] = threading.Event()
            self._dispatch_locked()
            self._save_locked()
            return record.to_dict()

    def _dispatch_locked(self):
        while len(self.running) < self.max_tasks and self.queue:
            task_id = self.queue.pop(0)
            task = self.tasks.get(task_id)
            if task is None or task.state != "queued":
                continue
            task.state = "running"
            task.started_at = utcnow()
            self.running.add(task_id)
            threading.Thread(
                target=self._run, args=(task_id,), daemon=True
            ).start()

    def _run(self, task_id):
        with self.lock:
            params = self._params.pop(task_id, None)
            cancel = self._cancels.get(task_id)
        if params is None or cancel is None:
            return

        options = DownloadOptions(**params)

        def on_progress(completed, total):
            with self.lock:
                task = self.tasks.get(task_id)
                if task is None or task.state != "running":
                    return
                task.completed = completed
                task.total = total
                self._dirty = True

        try:
            result = self.runner(
                options, on_progress=on_progress, cancel=cancel
            )
        except DownloadCancelled:
            self._finish(task_id, "cancelled")
        except Exception as exc:
            self._finish(task_id, "failed", error=str(exc))
        else:
            self._finish(
                task_id, "succeeded", final_path=str(result)
            )

    def _finish(self, task_id, state, error=None, final_path=None):
        with self.lock:
            task = self.tasks.get(task_id)
            self.running.discard(task_id)
            self._cancels.pop(task_id, None)
            if task is not None and task.state == "running":
                task.state = state
                task.finished_at = utcnow()
                task.error = error
                if final_path is not None:
                    task.final_path = final_path
            self._dispatch_locked()
            self._save_locked()

    def cancel(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            if task.state == "queued":
                self.queue.remove(task_id)
                task.state = "cancelled"
                task.finished_at = utcnow()
                self._params.pop(task_id, None)
                self._cancels.pop(task_id, None)
                self._save_locked()
                return task.to_dict()
            if task.state == "running":
                event = self._cancels.get(task_id)
                if event is not None:
                    event.set()
                return task.to_dict()
            raise TaskConflict(f"任务已结束：{task.state}")

    def get(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            return task.to_dict()

    def list(self, state=None, limit=50, offset=0):
        with self.lock:
            records = [
                self.tasks[i].to_dict()
                for i in reversed(self.order)
                if i in self.tasks
            ]
        if state is not None:
            records = [r for r in records if r["state"] == state]
        total = len(records)
        return records[offset:offset + limit], total

    def _load(self):
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for item in data.get("tasks", []):
            try:
                task = TaskRecord(**item)
            except TypeError:
                continue
            if task.state in ("queued", "running"):
                task.state = "interrupted"
                task.finished_at = task.finished_at or utcnow()
            self.tasks[task.id] = task
            self.order.append(task.id)
        self._prune_locked()

    def _prune_locked(self):
        terminal = [
            task_id for task_id in self.order
            if self.tasks.get(task_id) is not None
            and self.tasks[task_id].state in TERMINAL_STATES
        ]
        for task_id in terminal[:-KEEP_TERMINAL]:
            self.tasks.pop(task_id, None)
            self.order.remove(task_id)
            self._params.pop(task_id, None)
            self._cancels.pop(task_id, None)

    def _save_locked(self):
        payload = {
            "version": 1,
            "tasks": [
                self.tasks[i].to_dict()
                for i in self.order
                if i in self.tasks
            ],
        }
        temp = self.state_file.with_suffix(
            self.state_file.suffix + ".part"
        )
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                "utf-8",
            )
            temp.replace(self.state_file)
        except OSError as exc:
            print(f"状态保存失败：{exc}")
        self._dirty = False

    def _flush_loop(self):
        while not self._stop.wait(2):
            with self.lock:
                if self._dirty:
                    self._save_locked()

    def close(self):
        self._stop.set()
        if self._flusher.is_alive():
            self._flusher.join(timeout=1)
        with self.lock:
            self._save_locked()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_serve -v`
Expected: PASS（7 个测试）

- [ ] **Step 5: 提交**

```bash
git add src/m3u8_cli/serve.py tests/test_serve.py
git commit -m "添加 serve 任务管理器"
```

---

## Task 6: serve HTTP API

**Files:**
- Modify: `src/m3u8_cli/serve.py`（追加 HTTP 层）
- Test: `tests/test_serve_api.py`

- [ ] **Step 1: 写失败测试**

```python
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

import requests

from m3u8_cli.serve import TaskManager, make_handler


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = TaskManager(
            self.tmp.name,
            max_tasks=2,
            default_jobs=2,
            runner=lambda options, on_progress=None, cancel=None:
                options.output,
        )
        self.token = "test-token"
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.manager, self.token),
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.base = (
            f"http://127.0.0.1:{self.server.server_address[1]}"
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.close()
        self.tmp.cleanup()

    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_requires_token(self):
        response = requests.get(f"{self.base}/api/tasks")
        self.assertEqual(response.status_code, 401)

    def test_options_preflight(self):
        response = requests.options(f"{self.base}/api/tasks")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"], "*"
        )

    def test_create_list_and_get_task(self):
        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            json={
                "url": "https://example.com/a.m3u8",
                "format": "hls",
                "output": "movie",
            },
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task"]["id"]

        self.assertTrue(wait_for(
            lambda: requests.get(
                f"{self.base}/api/tasks/{task_id}",
                headers=self.auth(),
            ).json()["task"]["state"] == "succeeded"
        ))

        listing = requests.get(
            f"{self.base}/api/tasks", headers=self.auth()
        ).json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["tasks"][0]["id"], task_id)

    def test_invalid_url_rejected(self):
        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            json={"url": "ftp://example.com/a.m3u8"},
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_output_rejected(self):
        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            json={
                "url": "https://example.com/a.m3u8",
                "output": "../escape.mp4",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_existing_target_conflicts(self):
        from pathlib import Path

        target = Path(self.tmp.name) / "movie"
        target.mkdir()
        (target / "playlist.m3u8").write_text("old")

        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            json={
                "url": "https://example.com/a.m3u8",
                "format": "hls",
                "output": "movie",
            },
        )
        self.assertEqual(response.status_code, 409)

    def test_delete_terminal_task_conflicts(self):
        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            json={
                "url": "https://example.com/a.m3u8",
                "format": "hls",
                "output": "movie",
            },
        )
        task_id = response.json()["task"]["id"]
        self.assertTrue(wait_for(
            lambda: requests.get(
                f"{self.base}/api/tasks/{task_id}",
                headers=self.auth(),
            ).json()["task"]["state"] == "succeeded"
        ))

        response = requests.delete(
            f"{self.base}/api/tasks/{task_id}", headers=self.auth()
        )
        self.assertEqual(response.status_code, 409)

    def test_unknown_task_not_found(self):
        response = requests.get(
            f"{self.base}/api/tasks/missing", headers=self.auth()
        )
        self.assertEqual(response.status_code, 404)

    def test_oversized_body_rejected(self):
        response = requests.post(
            f"{self.base}/api/tasks",
            headers=self.auth(),
            data=b"x" * (1024 * 1024 + 1),
        )
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_serve_api -v`
Expected: FAIL，`ImportError: cannot import name 'make_handler'`

- [ ] **Step 3: 在 `serve.py` 追加 HTTP 层**

先在文件顶部补充导入：

```python
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
```

再在文件末尾追加：

```python
MAX_BODY = 1024 * 1024
MAX_HEADERS_BYTES = 16 * 1024


def _valid_output_name(name):
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and len(name) <= 200
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
    )


def _int_field(payload, name, default, minimum, maximum):
    value = payload.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RequestError(f"{name} 必须是整数")
    if value < minimum or value > maximum:
        raise RequestError(
            f"{name} 必须在 {minimum} 到 {maximum} 之间"
        )
    return value


def parse_task_request(payload, default_jobs=8):
    """校验 POST /api/tasks 的请求体并返回 submit 参数。"""
    if not isinstance(payload, dict):
        raise RequestError("请求体必须是 JSON 对象")

    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise RequestError("url 不能为空")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise RequestError("url 必须是 http 或 https 地址")

    fmt = payload.get("format", "mp4")
    if fmt not in ("mp4", "hls"):
        raise RequestError("format 必须是 mp4 或 hls")

    output = payload.get("output")
    if output is None:
        output = "output" if fmt == "hls" else "output.mp4"
    if not _valid_output_name(output):
        raise RequestError("output 必须是纯文件名")

    overwrite = payload.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise RequestError("overwrite 必须是布尔值")

    headers = payload.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in headers.items()
    ):
        raise RequestError("headers 必须是字符串映射")
    if sum(
        len(k) + len(v) for k, v in headers.items()
    ) > MAX_HEADERS_BYTES:
        raise RequestError("headers 过大")

    return {
        "url": url,
        "fmt": fmt,
        "output": output,
        "jobs": _int_field(payload, "jobs", default_jobs, 1, 64),
        "retries": _int_field(payload, "retries", 3, 0, 10),
        "timeout": _int_field(payload, "timeout", 30, 1, 600),
        "overwrite": overwrite,
        "headers": headers,
    }


def make_handler(manager, token):
    """创建绑定到指定 TaskManager 与 token 的请求处理器。"""
    valid_states = {
        "queued", "running", "succeeded",
        "failed", "cancelled", "interrupted",
    }

    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "m3u8-cli"

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization, Content-Type",
            )
            self.send_header(
                "Access-Control-Allow-Methods",
                "GET, POST, DELETE, OPTIONS",
            )

        def _send_json(self, status, payload):
            body = json.dumps(
                payload, ensure_ascii=False
            ).encode("utf-8")
            self.send_response(status)
            self.send_header(
                "Content-Type", "application/json; charset=utf-8"
            )
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            header = self.headers.get("Authorization", "")
            scheme, _, value = header.partition(" ")
            if scheme.lower() != "bearer":
                return False
            try:
                return hmac.compare_digest(
                    value.strip(), token
                )
            except TypeError:
                return False

        def _require_auth(self):
            if not self._authorized():
                self._send_json(401, {"error": "未授权"})
                return False
            return True

        def _path_parts(self):
            return urlsplit(self.path).path.rstrip("/").split("/")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._require_auth():
                return
            try:
                self._handle_get()
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})

        def _handle_get(self):
            parts = self._path_parts()

            if parts == ["", "api", "tasks"]:
                query = parse_qs(urlsplit(self.path).query)
                state = query.get("state", [None])[0]
                if state is not None and state not in valid_states:
                    self._send_json(400, {"error": "无效的 state"})
                    return
                try:
                    limit = int(query.get("limit", ["50"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                except ValueError:
                    self._send_json(
                        400, {"error": "limit/offset 必须是整数"}
                    )
                    return
                if limit < 1 or limit > 500 or offset < 0:
                    self._send_json(
                        400, {"error": "limit/offset 越界"}
                    )
                    return

                tasks, total = manager.list(state, limit, offset)
                self._send_json(
                    200, {"tasks": tasks, "total": total}
                )
                return

            if len(parts) == 4 and parts[:3] == ["", "api", "tasks"]:
                try:
                    task = manager.get(parts[3])
                except TaskNotFound:
                    self._send_json(404, {"error": "任务不存在"})
                    return
                self._send_json(200, {"task": task})
                return

            self._send_json(404, {"error": "未找到路径"})

        def do_POST(self):
            if not self._require_auth():
                return
            try:
                self._handle_post()
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})

        def _handle_post(self):
            if self._path_parts() != ["", "api", "tasks"]:
                self._send_json(404, {"error": "未找到路径"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(
                    400, {"error": "无效的 Content-Length"}
                )
                return
            if length <= 0:
                self._send_json(400, {"error": "请求体不能为空"})
                return
            if length > MAX_BODY:
                self._send_json(413, {"error": "请求体过大"})
                return

            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._send_json(
                    400, {"error": "请求体不是有效 JSON"}
                )
                return

            try:
                params = parse_task_request(
                    payload, manager.default_jobs
                )
                task = manager.submit(**params)
            except RequestError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except TaskConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return

            self._send_json(202, {"task": task})

        def do_DELETE(self):
            if not self._require_auth():
                return
            try:
                parts = self._path_parts()
                if len(parts) != 4 or parts[:3] != ["", "api", "tasks"]:
                    self._send_json(404, {"error": "未找到路径"})
                    return
                try:
                    task = manager.cancel(parts[3])
                except TaskNotFound:
                    self._send_json(404, {"error": "任务不存在"})
                    return
                except TaskConflict as exc:
                    self._send_json(409, {"error": str(exc)})
                    return
                self._send_json(200, {"task": task})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})

        def log_message(self, format, *args):
            print(f"[serve] {self.address_string()} {format % args}")

    return ApiHandler


def run_serve(args):
    """启动 HTTP 服务，直到 Ctrl+C。"""
    output_dir = Path(args.output_dir).expanduser().resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise HLSException(f"输出路径不是目录：{output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    token = args.token or secrets.token_urlsafe(24)
    state_file = (
        Path(args.state_file).expanduser()
        if args.state_file
        else output_dir / ".m3u8-serve-state.json"
    )
    manager = TaskManager(
        output_dir,
        max_tasks=args.max_tasks,
        default_jobs=args.jobs,
        state_file=state_file,
    )
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(manager, token)
    )
    server.daemon_threads = True

    print(
        f"监听地址：http://{args.host}:{server.server_address[1]}"
    )
    print(f"输出目录：{output_dir}")
    print(f"API token：{token}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.close()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_serve_api -v`
Expected: PASS（9 个测试）

- [ ] **Step 5: 提交**

```bash
git add src/m3u8_cli/serve.py tests/test_serve_api.py
git commit -m "添加 serve REST API"
```

---

## Task 7: serve 子命令、文档与端到端验证

**Files:**
- Modify: `src/m3u8_cli/cli.py:20-78,194-216`
- Modify: `.gitignore:12-24`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Test: `tests/test_cli.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_cli.py` 增加：

```python
class ServeParserTests(unittest.TestCase):
    def test_serve_defaults(self):
        args = build_parser().parse_args(["serve"])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 8787)
        self.assertIsNone(args.token)
        self.assertEqual(args.output_dir, ".")
        self.assertEqual(args.max_tasks, 3)
        self.assertEqual(args.jobs, 8)
        self.assertIsNone(args.state_file)

    def test_serve_rejects_invalid_limits(self):
        with mock.patch("sys.argv", ["m3u8-cli", "serve", "--port", "0"]), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main()
        self.assertEqual(ctx.exception.code, 2)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run python -m unittest tests.test_cli.ServeParserTests -v`
Expected: FAIL，argparse 不认识 `serve`

- [ ] **Step 3: 修改 `cli.py`**

在 `main()` 之前新增 `run_serve` 导入与子命令。文件顶部增加：

```python
from .serve import run_serve
```

在 `build_parser()` 的 `convert` 子命令之后增加：

```python
    serve = subparsers.add_parser(
        "serve",
        help="启动 RESTful 下载服务",
        description="在局域网启动 HTTP API，接受远程下载任务",
    )
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument(
        "--token", help="Bearer token；省略时自动生成并打印",
    )
    serve.add_argument(
        "--output-dir", default=".",
        help="任务目录根路径（默认当前目录）",
    )
    serve.add_argument(
        "--max-tasks", type=int, default=3,
        help="同时运行的任务数上限",
    )
    serve.add_argument(
        "--jobs", type=int, default=8,
        help="请求未指定 jobs 时的分片并发默认值",
    )
    serve.add_argument(
        "--state-file", help="任务状态文件路径",
    )
```

在 `main()` 的分支链中增加：

```python
    elif args.command == "serve":
        if not 1 <= args.port <= 65535:
            parser.error("port 必须在 1 到 65535 之间")
        if args.max_tasks < 1 or args.jobs < 1:
            parser.error("max-tasks/jobs 必须为正整数")
        handler = run_serve
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run python -m unittest tests.test_cli -v`
Expected: PASS

- [ ] **Step 5: 更新 `.gitignore`**

在“下载与转换产物”段追加：

```
.m3u8-serve-state.json
*.m3u8-serve-state.json
```

- [ ] **Step 6: 更新 `README.md`**

在下载参数表之前补充 `-f` 说明，并新增 `serve` 章节。至少包含：

````markdown
### 保留 HLS（不合并）

```bash
# 输出 movie/playlist.m3u8 与 movie/000000.ts 等分片
m3u8-cli download "<m3u8_url>" -f hls -o movie

# 源流加密时同时下载 key.bin，播放列表 URI 指向本地文件
```

- `-f hls` 不使用 FFmpeg；分片保持服务器返回的原始字节。
- 分片、`playlist.m3u8`、`key.bin`（如有）位于同一目录。
- fMP4 流输出 `init.mp4` 与 `%06d.m4s`。

### 启动下载服务

```bash
m3u8-cli serve --host 0.0.0.0 --port 8787 --output-dir ./downloads
```

启动日志会打印自动生成的 `API token`。

```bash
# 创建任务
curl -X POST http://127.0.0.1:8787/api/tasks \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/playlist.m3u8",
    "format": "hls",
    "output": "movie",
    "headers": {"Referer": "https://example.com/"}
  }'

# 任务列表 / 详情
curl http://127.0.0.1:8787/api/tasks -H "Authorization: Bearer <token>"
curl http://127.0.0.1:8787/api/tasks/<id> -H "Authorization: Bearer <token>"

# 取消任务
curl -X DELETE http://127.0.0.1:8787/api/tasks/<id> \
  -H "Authorization: Bearer <token>"
```

- 全局最多 `--max-tasks` 个任务同时运行，其余 FIFO 排队。
- 请求头通过 `headers` 对象传入，用于播放列表、分片与密钥请求，不会被持久化。
- 状态文件默认 `<output-dir>/.m3u8-serve-state.json`；重启后未完成任务标记为 `interrupted`。
````

- [ ] **Step 7: 更新 `CLAUDE.md`**

在模块划分中增加 `download.py`、`hls_export.py`、`serve.py` 的职责说明；在常用命令中增加：

```bash
# 保留 HLS（不合并）
uv run m3u8-cli download "<m3u8_url>" -f hls -o movie

# 启动 REST API
uv run m3u8-cli serve --host 0.0.0.0 --port 8787 --output-dir ./downloads
```

并更新“已知限制”：serve 不做 SSRF 屏蔽、不提供产物下载接口、重启后任务不自动恢复。

- [ ] **Step 8: 运行全部测试与编译检查**

Run:

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src
```

Expected: 全部 PASS，compileall 无输出

- [ ] **Step 9: 端到端验证 `-f hls`**

```bash
mkdir -p /tmp/hls-src /tmp/hls-out
ffmpeg -hide_banner -y -f lavfi -i testsrc=size=320x180:rate=10 -t 6 \
  -c:v libx264 -f hls -hls_time 2 -hls_playlist_type vod \
  /tmp/hls-src/playlist.m3u8
python -m http.server 8099 --directory /tmp/hls-src &
uv run m3u8-cli download "http://127.0.0.1:8099/playlist.m3u8" \
  -f hls -o /tmp/hls-out/movie
ls /tmp/hls-out/movie
ffprobe -v error -show_entries format=duration \
  /tmp/hls-out/movie/playlist.m3u8
kill %1
```

Expected：目录内出现 `playlist.m3u8`、`000000.ts` 等；ffprobe 能读取时长。

- [ ] **Step 10: 端到端验证 serve**

```bash
uv run m3u8-cli serve --host 127.0.0.1 --port 8787 \
  --output-dir /tmp/serve-out --token test-token &
sleep 1
curl -s -X POST http://127.0.0.1:8787/api/tasks \
  -H "Authorization: Bearer test-token" \
  -H "Content-Type: application/json" \
  -d '{"url":"http://127.0.0.1:8099/playlist.m3u8","format":"hls","output":"movie"}'
curl -s http://127.0.0.1:8787/api/tasks \
  -H "Authorization: Bearer test-token"
kill %1
```

Expected：POST 返回 202 与任务 `id`；列表接口能看到该任务，最终状态为 `succeeded`。

- [ ] **Step 11: 提交**

```bash
git add src/m3u8_cli/cli.py .gitignore README.md CLAUDE.md tests/test_cli.py
git commit -m "添加 serve 子命令与文档"
```

---

## 自检清单

- [ ] `download -f hls` 产物为 `<name>/playlist.m3u8` + 扁平分片 + `key.bin`。
- [ ] 加密源流保持密文，密钥本地化并重写 URI；未加密流不生成密钥。
- [ ] 播放列表保留媒体序列号、`EXTINF`、`EXT-X-MAP`；fMP4 使用 `.m4s`。
- [ ] `serve` 支持 Bearer 鉴权、CORS、异步任务、并发上限、FIFO、取消、列表与持久化。
- [ ] 请求头不落盘；重启后未完成任务标记为 `interrupted`。
- [ ] `-f mp4` 行为与产物布局保持不变。
- [ ] 全部单元测试、compileall 与两个端到端场景通过。

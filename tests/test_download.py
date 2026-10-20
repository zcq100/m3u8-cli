import contextlib
import io
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from m3u8_cli.download import (
    DownloadOptions,
    _probe_media,
    download_hls,
)
from m3u8_cli.errors import DownloadCancelled
from m3u8_cli.paths import temp_cache_dir


class FakeDownloader:
    decrypt_flags = []
    verify_flags = []

    def __init__(self, headers, timeout, retries, verify=True):
        FakeDownloader.verify_flags.append(verify)

    def download(self, url, path, key_info=None, sequence=0,
                 decrypt=True):
        FakeDownloader.decrypt_flags.append(decrypt)
        path.write_bytes(b"data")
        return path

    def get_key(self, uri):
        return b"k" * 16


PROBE_DATA = {
    "format": {"duration": "2.000000", "format_name": "mov,mp4"},
    "streams": [{"codec_name": "h264"}],
}


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
        FakeDownloader.verify_flags = []

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
                "m3u8_cli.download._probe_media",
                return_value=(PROBE_DATA, ""),
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
            # 合并成功后临时缓存被清理，元数据留在最终文件旁。
            self.assertFalse(temp_cache_dir(output).exists())
            meta = json.loads(
                (output.parent / (output.name + ".meta.json"))
                .read_text("utf-8")
            )
            self.assertEqual(meta["format"]["duration"], "2.000000")
            self.assertEqual(meta["streams"][0]["codec_name"], "h264")
            source = meta["source"]
            self.assertEqual(source["format"], "mp4")
            self.assertEqual(source["url"], "https://example.com/a.m3u8")
            self.assertEqual(source["segments"], 2)
            self.assertNotIn("output", source)
            self.assertNotIn(str(output), json.dumps(meta))

    def test_metadata_omits_request_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "movie.mp4"

            with mock.patch(
                "m3u8_cli.download.load_playlist",
                return_value=make_playlist(),
            ), mock.patch(
                "m3u8_cli.download.Downloader", FakeDownloader
            ), mock.patch(
                "m3u8_cli.download.combine_segments",
                return_value=Path(tmp) / "combined.ts",
            ), mock.patch(
                "m3u8_cli.download._probe_media",
                return_value=(PROBE_DATA, ""),
            ), mock.patch(
                "m3u8_cli.download.mux"
            ):
                download_hls(
                    DownloadOptions(
                        url="https://example.com/a.m3u8",
                        output=output,
                        headers={"Cookie": "session=secret"},
                    )
                )

            text = (
                output.parent / (output.name + ".meta.json")
            ).read_text("utf-8")
            self.assertNotIn("secret", text)
            self.assertNotIn("Cookie", text)

    def test_metadata_warns_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "movie.mp4"
            stderr = io.StringIO()

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
            ), mock.patch(
                "m3u8_cli.download.shutil.which",
                return_value=None,
            ), contextlib.redirect_stderr(stderr):
                download_hls(
                    DownloadOptions(
                        url="https://example.com/a.m3u8",
                        output=output,
                    )
                )

            self.assertIn("未找到 ffprobe", stderr.getvalue())
            meta = json.loads(
                (output.parent / (output.name + ".meta.json"))
                .read_text("utf-8")
            )
            self.assertNotIn("format", meta)
            self.assertIn("source", meta)
            self.assertNotIn("output", meta["source"])

    def test_probe_removes_filename_and_uses_hls_extensions(self):
        completed = mock.Mock(stdout=json.dumps({
            "format": {"filename": "/x/movie.mp4", "duration": "2.0"},
            "streams": [],
        }))

        with mock.patch(
            "m3u8_cli.download.shutil.which",
            return_value="/usr/bin/ffprobe",
        ), mock.patch(
            "m3u8_cli.download.subprocess.run",
            return_value=completed,
        ) as run:
            data, warning = _probe_media(Path("/x/movie.mp4"), "mp4")

        self.assertEqual(warning, "")
        self.assertNotIn("filename", data["format"])
        self.assertNotIn("-allowed_extensions", run.call_args.args[0])

        with mock.patch(
            "m3u8_cli.download.shutil.which",
            return_value="/usr/bin/ffprobe",
        ), mock.patch(
            "m3u8_cli.download.subprocess.run",
            return_value=completed,
        ) as run:
            _probe_media(Path("/x/playlist.m3u8"), "hls")

        self.assertIn("-allowed_extensions", run.call_args.args[0])

    def test_probe_handles_missing_stdout(self):
        completed = mock.Mock(stdout=None)

        with mock.patch(
            "m3u8_cli.download.shutil.which",
            return_value="/usr/bin/ffprobe",
        ), mock.patch(
            "m3u8_cli.download.subprocess.run",
            return_value=completed,
        ):
            data, warning = _probe_media(Path("/x/movie.mp4"), "mp4")

        self.assertIsNone(data)
        self.assertIn("未返回数据", warning)

    def test_metadata_failure_does_not_fail_download(self):
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "movie.mp4"

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
            ), mock.patch(
                "m3u8_cli.download._probe_media",
                side_effect=TypeError(
                    "the JSON object must be str, bytes or bytearray, "
                    "not NoneType"
                ),
            ), contextlib.redirect_stderr(stderr):
                result = download_hls(
                    DownloadOptions(
                        url="https://example.com/a.m3u8",
                        output=output,
                    )
                )

            self.assertEqual(result, output)
            self.assertIn("元数据生成失败", stderr.getvalue())
            self.assertFalse(
                (output.parent / (output.name + ".meta.json")).exists()
            )

    def test_mp4_failure_keeps_temp_cache_for_resume(self):
        cancel = threading.Event()

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "movie.mp4"
            cache = temp_cache_dir(output)

            def progress(done, total):
                cancel.set()

            try:
                with mock.patch(
                    "m3u8_cli.download.load_playlist",
                    return_value=make_playlist(segments=3),
                ), mock.patch(
                    "m3u8_cli.download.Downloader", FakeDownloader
                ):
                    with self.assertRaises(DownloadCancelled):
                        download_hls(
                            DownloadOptions(
                                url="https://example.com/a.m3u8",
                                output=output,
                                jobs=1,
                            ),
                            on_progress=progress,
                            cancel=cancel,
                        )

                self.assertTrue(
                    (cache / "segment_000000.bin").is_file()
                )
                self.assertFalse(output.exists())
            finally:
                shutil.rmtree(cache, ignore_errors=True)

    def test_insecure_propagates_to_playlist_and_downloader(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "movie.mp4"

            with mock.patch(
                "m3u8_cli.download.load_playlist",
                return_value=make_playlist(),
            ) as load, mock.patch(
                "m3u8_cli.download.Downloader", FakeDownloader
            ), mock.patch(
                "m3u8_cli.download.combine_segments",
                return_value=Path(tmp) / "combined.ts",
            ), mock.patch(
                "m3u8_cli.download._probe_media",
                return_value=(PROBE_DATA, ""),
            ), mock.patch(
                "m3u8_cli.download.mux"
            ):
                download_hls(
                    DownloadOptions(
                        url="https://example.com/a.m3u8",
                        output=output,
                        verify=False,
                    )
                )

            self.assertIs(load.call_args.kwargs["verify"], False)
            self.assertEqual(FakeDownloader.verify_flags, [False])
            meta = json.loads(
                (output.parent / (output.name + ".meta.json"))
                .read_text("utf-8")
            )
            self.assertIs(meta["source"]["verify_tls"], False)

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
            cache = temp_cache_dir(output)
            cache.mkdir(parents=True)
            (cache / "segment_000000.bin").write_bytes(b"old")
            (cache / "state.json").write_text(
                json.dumps({"fingerprint": "abc", "mode": "hls"}),
                "utf-8",
            )

            try:
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
                ), mock.patch(
                    "m3u8_cli.download._cleanup_cache"
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
                self.assertFalse(
                    (cache / "segment_000000.bin").exists()
                )
            finally:
                shutil.rmtree(cache, ignore_errors=True)

    def test_cancel_during_download_keeps_completed_segments(self):
        cancel = threading.Event()

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "movie"

            def progress(done, total):
                if done == 1:
                    cancel.set()

            with mock.patch(
                "m3u8_cli.download.load_playlist",
                return_value=make_playlist(segments=5),
            ), mock.patch(
                "m3u8_cli.download.Downloader", FakeDownloader
            ), mock.patch(
                "m3u8_cli.download.export_hls"
            ) as export:
                with self.assertRaises(DownloadCancelled):
                    download_hls(
                        DownloadOptions(
                            url="https://example.com/a.m3u8",
                            output=output,
                            format="hls",
                            jobs=1,
                        ),
                        on_progress=progress,
                        cancel=cancel,
                    )

            export.assert_not_called()
            cache = temp_cache_dir(output)
            try:
                self.assertTrue(
                    (cache / "segment_000000.bin").is_file()
                )
                self.assertFalse(
                    (cache / "segment_000002.bin").exists()
                )
                self.assertFalse((output / "playlist.m3u8").exists())
                self.assertFalse((output / "cache").exists())
            finally:
                shutil.rmtree(cache, ignore_errors=True)

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
            ), mock.patch(
                "m3u8_cli.download._probe_media",
                return_value=(PROBE_DATA, ""),
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
            meta = json.loads(
                (output / "meta.json").read_text("utf-8")
            )
            self.assertEqual(meta["source"]["format"], "hls")
            self.assertEqual(meta["source"]["segments"], 1)
            self.assertNotIn("output", meta["source"])
            # 导出成功后临时缓存被清理，输出目录里也没有 cache。
            self.assertFalse(temp_cache_dir(output).exists())
            self.assertFalse((output / "cache").exists())
            self.assertEqual(meta["streams"][0]["codec_name"], "h264")


if __name__ == "__main__":
    unittest.main()

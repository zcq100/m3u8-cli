import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from m3u8_cli.download import DownloadOptions, download_hls
from m3u8_cli.errors import DownloadCancelled
from m3u8_cli.paths import temp_cache_dir


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
            # 合并成功后临时缓存被清理，元数据留在最终文件旁。
            self.assertFalse(temp_cache_dir(output).exists())
            meta = json.loads(
                (output.parent / (output.name + ".meta.json"))
                .read_text("utf-8")
            )
            self.assertEqual(meta["format"], "mp4")
            self.assertEqual(meta["url"], "https://example.com/a.m3u8")
            self.assertEqual(meta["segments"], 2)
            self.assertEqual(meta["output"], str(output))

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
            cache = output / "cache"
            self.assertTrue((cache / "segment_000000.bin").is_file())
            self.assertFalse((cache / "segment_000002.bin").exists())
            self.assertFalse((output / "playlist.m3u8").exists())

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
            meta = json.loads(
                (output / "meta.json").read_text("utf-8")
            )
            self.assertEqual(meta["format"], "hls")
            self.assertEqual(meta["segments"], 1)
            self.assertEqual(meta["output"], str(output))


if __name__ == "__main__":
    unittest.main()

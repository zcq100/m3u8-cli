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
                    key_info={"uri": "https://example.com/key.bin"},
                    sequence=3,
                    decrypt=False,
                )

            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), ciphertext)

    def test_key_length_validated(self):
        downloader = Downloader({}, 30, 0)
        for data in (b"short", b"k" * 17):
            with mock.patch.object(
                downloader, "get_bytes", return_value=data
            ):
                with self.assertRaises(HLSException):
                    downloader.get_key(
                        "https://example.com/key.bin"
                    )

    def test_default_mode_decrypts(self):
        downloader = Downloader({}, 30, 0)
        key_info = {"uri": "https://example.com/key.bin"}

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "segment.bin"
            with mock.patch.object(
                downloader, "get_bytes", return_value=b"cipher"
            ), mock.patch.object(
                downloader,
                "decrypt",
                return_value=b"plain",
            ) as decrypt:
                downloader.download(
                    "https://example.com/s.ts",
                    target,
                    key_info,
                    7,
                )

            decrypt.assert_called_once_with(b"cipher", key_info, 7)
            self.assertEqual(target.read_bytes(), b"plain")


if __name__ == "__main__":
    unittest.main()

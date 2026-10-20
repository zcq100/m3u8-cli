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

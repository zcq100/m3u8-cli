import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from m3u8_cli.cli import build_parser, main


class DownloadFlowTests(unittest.TestCase):
    def _run(self, argv, fmt):
        with mock.patch(
            "m3u8_cli.cli.download_hls"
        ) as download, mock.patch(
            "m3u8_cli.cli.shutil.which",
            return_value="/usr/bin/ffmpeg",
        ), mock.patch("sys.argv", argv), \
                contextlib.redirect_stdout(io.StringIO()):
            main()

        options = download.call_args.args[0]
        self.assertEqual(options.output, Path("output.mp4")
                         if fmt == "mp4" else Path("output"))

    def test_mp4_default_output(self):
        self._run(["m3u8-cli", "download", "https://example.com/a.m3u8"],
                  "mp4")

    def test_hls_default_output(self):
        self._run(
            ["m3u8-cli", "download", "https://example.com/a.m3u8",
             "-f", "hls"],
            "hls",
        )


class DownloadParserTests(unittest.TestCase):
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

    def test_subcommand_required(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])


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
        self.assertFalse(args.encrypt)

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

    def test_convert_encrypt_flag(self):
        args = build_parser().parse_args(
            ["convert", "movie.mp4", "--encrypt"]
        )
        self.assertTrue(args.encrypt)


class MainValidationTests(unittest.TestCase):
    def test_encrypt_rejected_for_non_hls(self):
        with mock.patch("m3u8_cli.cli.convert_video") as convert, \
                mock.patch(
                    "sys.argv",
                    [
                        "m3u8-cli", "convert", "movie.mp4",
                        "-f", "mp4", "--encrypt",
                    ],
                ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as ctx:
                main()

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--encrypt 仅适用于 HLS", stderr.getvalue())
        convert.assert_not_called()

    def test_encrypt_passed_to_convert_video(self):
        with mock.patch("m3u8_cli.cli.convert_video") as convert, \
                mock.patch(
                    "m3u8_cli.cli.shutil.which",
                    return_value="/usr/bin/ffmpeg",
                ), mock.patch(
                    "sys.argv",
                    ["m3u8-cli", "convert", "movie.mp4", "--encrypt"],
                ), contextlib.redirect_stdout(io.StringIO()):
            main()

        self.assertTrue(convert.call_args.args[6])


if __name__ == "__main__":
    unittest.main()

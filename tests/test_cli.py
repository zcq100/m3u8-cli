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


if __name__ == "__main__":
    unittest.main()

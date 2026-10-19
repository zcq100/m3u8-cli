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

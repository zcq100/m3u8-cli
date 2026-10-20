import unittest
from unittest import mock

from m3u8_cli.errors import HLSException
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
        self.assertEqual(segments[0]["key"]["iv"],
                         "0x0123456789abcdef0123456789abcdef")
        self.assertEqual(playlist["maps"], [])

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

    def test_malformed_extinf_rejected(self):
        text = """#EXTM3U
#EXTINF:abc,
s.ts
#EXT-X-ENDLIST
"""
        with mock.patch(
            "m3u8_cli.playlist.requests.get",
            return_value=FakeResponse(text),
        ):
            with self.assertRaisesRegex(
                HLSException, "无效的 EXTINF"
            ):
                load_playlist("https://example.com/a.m3u8", {}, 30)

    def test_non_finite_extinf_rejected(self):
        text = """#EXTM3U
#EXTINF:nan,
s.ts
#EXT-X-ENDLIST
"""
        with mock.patch(
            "m3u8_cli.playlist.requests.get",
            return_value=FakeResponse(text),
        ):
            with self.assertRaisesRegex(
                HLSException, "无效的 EXTINF"
            ):
                load_playlist("https://example.com/a.m3u8", {}, 30)


if __name__ == "__main__":
    unittest.main()

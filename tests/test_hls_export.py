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

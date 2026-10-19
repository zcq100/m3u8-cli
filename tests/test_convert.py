import os
import tempfile
import unittest
from pathlib import Path

from m3u8_cli.convert import (
    build_file_command,
    build_hls_command,
    resolve_output_dir,
)
from m3u8_cli.errors import HLSException


class OutputDirTests(unittest.TestCase):
    def test_default_uses_input_stem(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            os.chdir(work)
            try:
                input_path = work / "movie.mp4"
                input_path.write_bytes(b"x")
                self.assertEqual(
                    resolve_output_dir(input_path, None),
                    work / "movie",
                )
                self.assertTrue((work / "movie").is_dir())
            finally:
                os.chdir(old_cwd)

    def test_custom_output_dir_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "out"
            self.assertEqual(
                resolve_output_dir(Path("movie.mp4"), str(out)),
                out,
            )
            self.assertTrue(out.is_dir())

    def test_output_path_is_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out"
            target.write_bytes(b"x")
            with self.assertRaises(HLSException):
                resolve_output_dir(Path("movie.mp4"), str(target))


class CommandTests(unittest.TestCase):
    def test_hls_h264_forces_keyframes(self):
        command = build_hls_command(
            Path("/videos/movie.mp4"), Path("/out"), "h264", 6
        )
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertIn("-force_key_frames", command)
        self.assertIn("expr:gte(t,n_forced*6)", command)
        self.assertIn("/out/ts/%06d.ts", command)

    def test_hls_copy_does_not_force_keyframes(self):
        command = build_hls_command(
            Path("/videos/movie.mp4"), Path("/out"), "copy", 6
        )
        self.assertIn("copy", command)
        self.assertNotIn("-force_key_frames", command)

    def test_webm_uses_vp9_opus(self):
        command = build_file_command(
            Path("/videos/movie.mp4"), "webm", "h264",
            Path("/out/movie.partial.webm"),
        )
        self.assertIn("libvpx-vp9", command)
        self.assertIn("libopus", command)
        self.assertNotIn("libx264", command)

    def test_mp4_hevc_uses_hvc1_tag(self):
        command = build_file_command(
            Path("/videos/movie.mp4"), "mp4", "hevc",
            Path("/out/movie.partial.mp4"),
        )
        self.assertIn("libx265", command)
        self.assertIn("-tag:v", command)
        self.assertIn("hvc1", command)
        self.assertIn("+faststart", command)


if __name__ == "__main__":
    unittest.main()

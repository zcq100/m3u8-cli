import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from m3u8_cli.convert import (
    build_file_command,
    build_hls_command,
    convert_video,
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

        # 分片位于 ts/ 子目录，播放列表必须带 base_url 才能定位分片。
        self.assertIn("-hls_base_url", command)
        base_url_index = command.index("-hls_base_url")
        self.assertEqual(command[base_url_index + 1], "ts/")

        self.assertEqual(command[-1], "/out/playlist.m3u8")

        # 视频/音频映射必须按 0:v?、0:a? 的顺序出现。
        self.assertIn("-map", command)
        video_index = command.index("0:v?")
        self.assertEqual(command[video_index - 1], "-map")
        self.assertEqual(command[video_index + 1], "-map")
        self.assertEqual(command[video_index + 2], "0:a?")

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

        # VP9 恒定质量参数必须按 -crf 32 -b:v 0 的顺序出现。
        crf_index = command.index("-crf")
        self.assertEqual(command[crf_index + 1], "32")
        self.assertEqual(command[crf_index + 2], "-b:v")
        self.assertEqual(command[crf_index + 3], "0")

    def test_mp4_hevc_uses_hvc1_tag(self):
        command = build_file_command(
            Path("/videos/movie.mp4"), "mp4", "hevc",
            Path("/out/movie.partial.mp4"),
        )
        self.assertIn("libx265", command)
        self.assertIn("-tag:v", command)
        self.assertIn("hvc1", command)
        self.assertIn("+faststart", command)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "需要 ffmpeg/ffprobe",
)
class HlsIntegrationTests(unittest.TestCase):
    def test_playlist_references_ts_directory(self):
        """实际调用 FFmpeg，确认播放列表中的分片路径可被 ffprobe 解析。"""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            sample = work / "sample.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-y",
                    "-f", "lavfi",
                    "-i", "testsrc2=size=160x120:rate=25",
                    "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(sample),
                ],
                check=True,
                capture_output=True,
            )

            out = work / "out"
            (out / "ts").mkdir(parents=True)
            subprocess.run(
                build_hls_command(sample, out, "h264", 1),
                check=True,
                capture_output=True,
            )

            playlist = out / "playlist.m3u8"
            self.assertIn("ts/000000.ts", playlist.read_text())

            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=format_name",
                    "-of", "default=nw=1",
                    str(playlist),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("hls", probe.stdout)


class ConvertVideoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.input = self.dir / "movie.mp4"
        self.input.write_bytes(b"fake")

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_input(self):
        with self.assertRaises(HLSException):
            convert_video(
                self.dir / "missing.mp4", "hls", "h264", None, 6, False
            )

    def test_hls_existing_target_without_overwrite(self):
        out = self.dir / "out"
        out.mkdir()
        (out / "playlist.m3u8").write_text("old", encoding="utf-8")
        with self.assertRaises(HLSException):
            convert_video(self.input, "hls", "h264", str(out), 6, False)

    def test_ts_path_is_file(self):
        out = self.dir / "out"
        out.mkdir()
        (out / "ts").write_bytes(b"not a dir")
        with self.assertRaises(HLSException):
            convert_video(self.input, "hls", "h264", str(out), 6, True)

    @mock.patch("m3u8_cli.convert.subprocess.run")
    def test_hls_overwrite_clears_old_segments(self, run):
        out = self.dir / "out"
        (out / "ts").mkdir(parents=True)
        (out / "ts" / "000000.ts").write_bytes(b"old")
        (out / "playlist.m3u8").write_text("old", encoding="utf-8")

        result = convert_video(
            self.input, "hls", "h264", str(out), 6, True
        )

        self.assertEqual(result, out / "playlist.m3u8")
        self.assertFalse((out / "ts" / "000000.ts").exists())
        self.assertFalse((out / "playlist.m3u8").exists())
        self.assertTrue((out / "ts").is_dir())
        run.assert_called_once()

    @mock.patch("m3u8_cli.convert.subprocess.run")
    def test_single_file_uses_partial_then_replace(self, run):
        def fake_run(command, check):
            Path(command[-1]).write_bytes(b"new")

        run.side_effect = fake_run
        out = self.dir / "out"

        result = convert_video(
            self.input, "mkv", "h264", str(out), 6, False
        )

        self.assertEqual(result, out / "movie.mkv")
        self.assertEqual(result.read_bytes(), b"new")
        self.assertFalse((out / "movie.partial.mkv").exists())

    def test_output_equals_input_rejected(self):
        with self.assertRaises(HLSException):
            convert_video(
                self.input, "mp4", "h264", str(self.dir), 6, True
            )


if __name__ == "__main__":
    unittest.main()

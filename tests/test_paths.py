import os
import tempfile
import unittest
from pathlib import Path

from m3u8_cli.errors import HLSException
from m3u8_cli.paths import fit_output_name, temp_cache_dir


class FitOutputNameTests(unittest.TestCase):
    def test_short_name_unchanged(self):
        self.assertEqual(
            fit_output_name("movie.mp4", "/tmp/out", windows=False),
            "movie.mp4",
        )
        self.assertEqual(
            fit_output_name("movie.mp4", r"C:/Users/tester/Downloads",
                            windows=True),
            "movie.mp4",
        )

    def test_windows_long_title_stays_within_max_path(self):
        title = "标" * 200 + ".mp4"
        out_dir = (
            "C:/Users/tester/Documents/projects/"
            "very-long-directory-name/downloads"
        )
        fitted = fit_output_name(title, out_dir, windows=True)

        self.assertLess(len(fitted), len(title))
        self.assertTrue(fitted.endswith(".mp4"))
        stem = Path(fitted).stem
        final = Path(out_dir) / fitted
        paths = [
            final,
            final.parent / (stem + ".partial.mp4"),
            temp_cache_dir(final) / "segment_000000.bin",
        ]
        for path in paths:
            self.assertLessEqual(
                len(str(path).encode("utf-16-le")) // 2, 259
            )

    def test_windows_title_that_fits_is_kept(self):
        title = "标" * 150 + ".mp4"
        self.assertEqual(
            fit_output_name(title, r"C:/Users/tester/Downloads",
                            windows=True),
            title,
        )

    def test_posix_component_fits_255_bytes(self):
        title = "视" * 200 + ".mp4"
        fitted = fit_output_name(title, "/tmp/out", windows=False)

        self.assertLess(len(fitted), len(title))
        self.assertTrue(fitted.endswith(".mp4"))
        self.assertLessEqual(len(os.fsencode(fitted)), 255)

    def test_unfixable_output_dir_raises(self):
        out_dir = "C:/" + "深" * 250
        with self.assertRaises(HLSException):
            fit_output_name("movie.mp4", out_dir, windows=True)

    def test_result_is_deterministic(self):
        title = "长" * 150 + ".mp4"
        first = fit_output_name(title, "/tmp/out", windows=False)
        second = fit_output_name(title, "/tmp/out", windows=False)
        self.assertEqual(first, second)


class TempCacheDirTests(unittest.TestCase):
    def test_deterministic_per_final_path(self):
        base = Path(tempfile.gettempdir())
        first = temp_cache_dir("/tmp/out/movie.mp4")
        second = temp_cache_dir("/tmp/out/movie.mp4")
        other = temp_cache_dir("/tmp/out/other.mp4")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertTrue(first.is_relative_to(base))

    def test_hls_names_are_not_used(self):
        # 同一个 stem 的 mp4 与 hls 任务最终路径不同，缓存也必须不同。
        self.assertNotEqual(
            temp_cache_dir("/tmp/out/movie.mp4"),
            temp_cache_dir("/tmp/out/movie"),
        )


if __name__ == "__main__":
    unittest.main()

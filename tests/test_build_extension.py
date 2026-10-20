import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = ROOT / "chrome-extension"
SCRIPT = ROOT / "scripts" / "build_extension.py"

EXPECTED_FILES = [
    "manifest.json",
    "lib.js",
    "background.js",
    "content.js",
    "popup.html",
    "popup.css",
    "popup.js",
]


def run_build(out_dir: Path, extension_dir: Path = EXTENSION_DIR):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--out-dir",
            str(out_dir),
            "--extension-dir",
            str(extension_dir),
        ],
        capture_output=True,
        text=True,
    )


class BuildExtensionTests(unittest.TestCase):
    def test_builds_runtime_only_zip(self):
        manifest = json.loads(
            (EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = run_build(out_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            zips = list(out_dir.glob("*.zip"))
            self.assertEqual(len(zips), 1)
            zip_path = zips[0]
            self.assertEqual(
                zip_path.name,
                f"m3u8-cli-chrome-extension-{manifest['version']}.zip",
            )

            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(archive.namelist(), EXPECTED_FILES)
                self.assertEqual(
                    json.loads(archive.read("manifest.json")),
                    manifest,
                )

    def test_build_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            first = run_build(out_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            zip_path = next(out_dir.glob("*.zip"))
            first_bytes = zip_path.read_bytes()

            second = run_build(out_dir)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(zip_path.read_bytes(), first_bytes)

    def test_rejects_invalid_manifest_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            extension_dir = Path(tmp) / "extension"
            shutil.copytree(EXTENSION_DIR, extension_dir)
            manifest_path = extension_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = "1.0.0-beta"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )

            result = run_build(Path(tmp) / "out", extension_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("版本", result.stderr)
            self.assertFalse(list((Path(tmp) / "out").glob("*.zip")))

    def test_rejects_missing_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            extension_dir = Path(tmp) / "extension"
            shutil.copytree(EXTENSION_DIR, extension_dir)
            (extension_dir / "popup.css").unlink()

            result = run_build(Path(tmp) / "out", extension_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("popup.css", result.stderr)
            self.assertFalse(list((Path(tmp) / "out").glob("*.zip")))


if __name__ == "__main__":
    unittest.main()

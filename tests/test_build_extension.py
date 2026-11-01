import io
import json
import os
import shutil
import struct
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


def run_build(
    out_dir: Path,
    extension_dir: Path = EXTENSION_DIR,
    key_file: Path | None = None,
    env_overrides: dict[str, str] | None = None,
):
    env = os.environ.copy()
    for name in ("CRX_PRIVATE_KEY", "CRX_PRIVATE_KEY_FILE"):
        env.pop(name, None)
    if env_overrides:
        env.update(env_overrides)

    command = [
        sys.executable,
        str(SCRIPT),
        "--out-dir",
        str(out_dir),
        "--extension-dir",
        str(extension_dir),
    ]
    if key_file is not None:
        command += ["--key-file", str(key_file)]
    return subprocess.run(command, capture_output=True, text=True, env=env)


class BuildExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("需要 openssl")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.key_path = Path(cls._tmp.name) / "key.pem"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(cls.key_path), "2048"],
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_builds_signed_crx(self):
        manifest = json.loads(
            (EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = run_build(out_dir, key_file=self.key_path)
            self.assertEqual(result.returncode, 0, result.stderr)

            crx_files = list(out_dir.glob("*.crx"))
            self.assertEqual(len(crx_files), 1)
            crx_path = crx_files[0]
            self.assertEqual(
                crx_path.name,
                f"m3u8-cli-chrome-extension-{manifest['version']}.crx",
            )
            self.assertFalse(list(out_dir.glob("*.zip")))

            data = crx_path.read_bytes()
            self.assertEqual(data[:4], b"Cr24")
            version, header_size = struct.unpack("<II", data[4:12])
            self.assertEqual(version, 3)

            payload = data[12 + header_size :]
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                self.assertEqual(archive.namelist(), EXPECTED_FILES)
                self.assertEqual(
                    json.loads(archive.read("manifest.json")),
                    manifest,
                )

    def test_build_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            first = run_build(out_dir, key_file=self.key_path)
            self.assertEqual(first.returncode, 0, first.stderr)
            crx_path = next(out_dir.glob("*.crx"))
            first_bytes = crx_path.read_bytes()

            second = run_build(out_dir, key_file=self.key_path)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(crx_path.read_bytes(), first_bytes)

    def test_reads_key_from_environment(self):
        key_content = self.key_path.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = run_build(
                out_dir, env_overrides={"CRX_PRIVATE_KEY": key_content}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(list(out_dir.glob("*.crx"))), 1)

    def test_rejects_missing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = run_build(out_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("私钥", result.stderr)
            self.assertFalse(list(out_dir.glob("*.crx")))

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

            result = run_build(
                Path(tmp) / "out", extension_dir, key_file=self.key_path
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("版本", result.stderr)
            self.assertFalse(list((Path(tmp) / "out").glob("*.crx")))

    def test_rejects_missing_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            extension_dir = Path(tmp) / "extension"
            shutil.copytree(EXTENSION_DIR, extension_dir)
            (extension_dir / "popup.css").unlink()

            result = run_build(
                Path(tmp) / "out", extension_dir, key_file=self.key_path
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("popup.css", result.stderr)
            self.assertFalse(list((Path(tmp) / "out").glob("*.crx")))


if __name__ == "__main__":
    unittest.main()

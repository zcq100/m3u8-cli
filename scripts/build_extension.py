#!/usr/bin/env python3
"""把 chrome-extension/ 打包为可加载的 Chrome 扩展 zip。

只包含运行时文件，使用固定时间戳，保证相同源码生成字节一致的产物。
输出默认写入 dist/，该目录已被 .gitignore 忽略。
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXTENSION_DIR = ROOT / "chrome-extension"
DEFAULT_OUT_DIR = ROOT / "dist"

# 扩展发布包内的运行时文件；manifest 中新增引用时需同步此列表。
RUNTIME_FILES = (
    "manifest.json",
    "lib.js",
    "background.js",
    "content.js",
    "popup.html",
    "popup.css",
    "popup.js",
)

REQUIRED_MANIFEST_KEYS = (
    "manifest_version",
    "name",
    "version",
    "background",
    "action",
    "content_scripts",
)

FIXED_DATE_TIME = (2020, 1, 1, 0, 0, 0)


def load_manifest(extension_dir: Path) -> dict:
    manifest_path = extension_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"找不到 {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{manifest_path} 不是合法 JSON：{exc}") from exc
    if not isinstance(manifest, dict):
        raise SystemExit(f"{manifest_path} 顶层必须是 JSON 对象")
    return manifest


def validate_version(version: object) -> str:
    if not isinstance(version, str):
        raise SystemExit("manifest.json 缺少字符串类型的 version")
    parts = version.split(".")
    if not 1 <= len(parts) <= 4:
        raise SystemExit("Chrome 扩展版本必须是 1-4 段数字，例如 1.0.0")
    for part in parts:
        if not part.isdigit() or (len(part) > 1 and part.startswith("0")):
            raise SystemExit("Chrome 扩展版本必须是 1-4 段数字，例如 1.0.0")
        if int(part) > 65535:
            raise SystemExit("Chrome 扩展版本的每段不能超过 65535")
    return version


def referenced_runtime_files(manifest: dict) -> set[str]:
    referenced = {"manifest.json"}
    background = manifest.get("background")
    if isinstance(background, dict) and isinstance(
        background.get("service_worker"), str
    ):
        referenced.add(background["service_worker"])
    action = manifest.get("action")
    if isinstance(action, dict) and isinstance(action.get("default_popup"), str):
        referenced.add(action["default_popup"])
    scripts = manifest.get("content_scripts")
    if isinstance(scripts, list):
        for entry in scripts:
            if not isinstance(entry, dict):
                continue
            for name in entry.get("js", []):
                if isinstance(name, str):
                    referenced.add(name)
    return referenced


def validate_manifest(manifest: dict, extension_dir: Path) -> str:
    missing = [
        key for key in REQUIRED_MANIFEST_KEYS if key not in manifest
    ]
    if missing:
        raise SystemExit(
            "manifest.json 缺少字段：" + ", ".join(missing)
        )
    if manifest.get("manifest_version") != 3:
        raise SystemExit("manifest.json 的 manifest_version 必须为 3")

    version = validate_version(manifest.get("version"))

    packaged = set(RUNTIME_FILES)
    unreferenced = referenced_runtime_files(manifest) - packaged
    if unreferenced:
        raise SystemExit(
            "打包清单缺少 manifest 引用的文件，请更新 RUNTIME_FILES："
            + ", ".join(sorted(unreferenced))
        )

    missing_files = [
        name for name in RUNTIME_FILES if not (extension_dir / name).is_file()
    ]
    if missing_files:
        raise SystemExit(
            "扩展目录缺少必需文件：" + ", ".join(missing_files)
        )
    return version


def write_zip(zip_path: Path, extension_dir: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = zip_path.with_name(zip_path.name + ".partial")
    try:
        with zipfile.ZipFile(
            partial_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name in RUNTIME_FILES:
                info = zipfile.ZipInfo(name, FIXED_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, (extension_dir / name).read_bytes())
        partial_path.replace(zip_path)
    finally:
        partial_path.unlink(missing_ok=True)


def build_extension(
    out_dir: Path = DEFAULT_OUT_DIR,
    extension_dir: Path = DEFAULT_EXTENSION_DIR,
) -> Path:
    manifest = load_manifest(extension_dir)
    version = validate_manifest(manifest, extension_dir)
    zip_path = out_dir / f"m3u8-cli-chrome-extension-{version}.zip"
    write_zip(zip_path, extension_dir)
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 chrome-extension/ 打包为 Chrome 可加载的 zip"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"输出目录（默认 {DEFAULT_OUT_DIR.relative_to(ROOT)}）",
    )
    parser.add_argument(
        "--extension-dir",
        type=Path,
        default=DEFAULT_EXTENSION_DIR,
        help="扩展源码目录（默认 chrome-extension/）",
    )
    args = parser.parse_args(argv)

    zip_path = build_extension(args.out_dir, args.extension_dir)
    size = zip_path.stat().st_size
    print(f"已生成 {zip_path}（{size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

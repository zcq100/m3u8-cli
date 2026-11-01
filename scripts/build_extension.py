#!/usr/bin/env python3
"""把 chrome-extension/ 打包并签名为 Chrome 可安装的 CRX3 文件。

只包含运行时文件，使用固定时间戳与 RSA PKCS#1 v1.5 签名，保证相同源码与私钥
生成字节一致的产物。私钥通过 --key-file、CRX_PRIVATE_KEY_FILE 或 CRX_PRIVATE_KEY
环境变量提供，绝不写入仓库。输出默认写入 dist/，该目录已被 .gitignore 忽略。

依赖 PATH 中的 openssl，不使用第三方 Python 包。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
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

CRX_MAGIC = b"Cr24"
CRX_VERSION = 3
# Chromium 对 CRX3 签名内容的固定前缀。
SIGNATURE_CONTEXT = b"CRX3 SignedData\x00"
KEY_FILE_ENV = "CRX_PRIVATE_KEY_FILE"
KEY_ENV = "CRX_PRIVATE_KEY"


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


def build_zip_data(extension_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for name in RUNTIME_FILES:
            info = zipfile.ZipInfo(name, FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (extension_dir / name).read_bytes())
    return buffer.getvalue()


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field_bytes(field: int, data: bytes) -> bytes:
    """编码 protobuf 的 length-delimited 字段。"""
    return varint((field << 3) | 2) + varint(len(data)) + data


def run_openssl(args: list[str], input_data: bytes | None = None) -> bytes:
    try:
        return subprocess.run(
            ["openssl", *args],
            input=input_data,
            check=True,
            capture_output=True,
        ).stdout
    except FileNotFoundError as exc:
        raise SystemExit("找不到 openssl，请先安装 OpenSSL") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"openssl 执行失败：{detail}") from exc


def public_key_der(key_path: Path) -> bytes:
    return run_openssl(
        ["rsa", "-in", str(key_path), "-pubout", "-outform", "DER"]
    )


def build_crx_data(zip_data: bytes, key_path: Path) -> bytes:
    public_key = public_key_der(key_path)
    crx_id = hashlib.sha256(public_key).digest()[:16]

    # SignedData { bytes crx_id = 1; }
    signed_header_data = field_bytes(1, crx_id)
    signed_payload = (
        SIGNATURE_CONTEXT
        + struct.pack("<I", len(signed_header_data))
        + signed_header_data
        + zip_data
    )
    signature = run_openssl(
        ["dgst", "-sha256", "-sign", str(key_path)], input_data=signed_payload
    )

    # AsymmetricKeyProof { bytes public_key = 1; bytes signature = 2; }
    proof = field_bytes(1, public_key) + field_bytes(2, signature)
    # CrxFileHeader { repeated sha256_with_rsa = 2; bytes signed_header_data = 10000; }
    header = field_bytes(2, proof) + field_bytes(10000, signed_header_data)

    return (
        CRX_MAGIC
        + struct.pack("<I", CRX_VERSION)
        + struct.pack("<I", len(header))
        + header
        + zip_data
    )


def resolve_key_path(key_file: Path | None) -> tuple[Path, Path | None]:
    """返回私钥路径与需要清理的临时文件。"""
    if key_file is not None:
        if not key_file.is_file():
            raise SystemExit(f"找不到私钥文件：{key_file}")
        return key_file, None

    env_file = os.environ.get(KEY_FILE_ENV)
    if env_file:
        path = Path(env_file)
        if not path.is_file():
            raise SystemExit(f"找不到私钥文件：{path}")
        return path, None

    content = os.environ.get(KEY_ENV)
    if content:
        if not content.endswith("\n"):
            content += "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix="m3u8-cli-crx-key-", suffix=".pem"
        )
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        return Path(temp_name), Path(temp_name)

    raise SystemExit(
        "缺少签名私钥：请用 --key-file 指定 PEM 文件，"
        f"或设置 {KEY_FILE_ENV} / {KEY_ENV} 环境变量"
    )


def write_output(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = path.with_name(path.name + ".partial")
    try:
        partial_path.write_bytes(data)
        partial_path.replace(path)
    finally:
        partial_path.unlink(missing_ok=True)


def build_extension(
    out_dir: Path = DEFAULT_OUT_DIR,
    extension_dir: Path = DEFAULT_EXTENSION_DIR,
    key_file: Path | None = None,
) -> Path:
    manifest = load_manifest(extension_dir)
    version = validate_manifest(manifest, extension_dir)
    key_path, cleanup = resolve_key_path(key_file)
    try:
        crx_data = build_crx_data(build_zip_data(extension_dir), key_path)
    finally:
        if cleanup is not None:
            cleanup.unlink(missing_ok=True)

    crx_path = out_dir / f"m3u8-cli-chrome-extension-{version}.crx"
    write_output(crx_path, crx_data)
    return crx_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 chrome-extension/ 打包并签名为 CRX3 文件"
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
    parser.add_argument(
        "--key-file",
        type=Path,
        default=None,
        help=(
            "RSA 私钥 PEM 文件；省略时依次读取 "
            f"{KEY_FILE_ENV} 与 {KEY_ENV} 环境变量"
        ),
    )
    args = parser.parse_args(argv)

    crx_path = build_extension(args.out_dir, args.extension_dir, args.key_file)
    size = crx_path.stat().st_size
    print(f"已生成 {crx_path}（{size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

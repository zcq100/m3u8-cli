"""任务子目录路径派生。"""

import hashlib
import os
import tempfile
from pathlib import Path

from .errors import HLSException

# Windows 默认 MAX_PATH=260（含结尾 NUL），超限后 mkdir/ffmpeg 会报“找不到文件”。
WINDOWS_PATH_LIMIT = 259
# NTFS 单个文件名上限为 255 UTF-16 单元；常见 POSIX 文件系统为 255 字节。
NAME_LIMIT = 255
CACHE_DIR = "cache"
PARTIAL_SUFFIX = ".partial"
# 各布局中最深的缓存文件名，用于 Windows 路径预算。
LONGEST_CACHE_FILE = "segment_000000.bin"


def derive_task_paths(output_arg):
    """返回最终文件路径和缓存目录，不创建任何目录。"""
    output = Path(output_arg).expanduser().resolve()

    # -o output/name.mp4 时直接复用 output/，避免嵌套成 output/output/。
    if output.parent.name == output.stem:
        task_dir = output.parent
    else:
        task_dir = output.parent / output.stem

    return output, task_dir / "cache"


def resolve_task_paths(output_arg):
    """返回任务目录内的最终文件路径和缓存目录，并创建任务目录。"""
    output, cache = derive_task_paths(output_arg)
    task_dir = cache.parent

    if task_dir.exists() and not task_dir.is_dir():
        raise HLSException(f"任务目录路径不是目录：{task_dir}")

    task_dir.mkdir(parents=True, exist_ok=True)
    return output, cache


def temp_cache_dir(final_path):
    """合并输出（mp4）的缓存目录：位于系统临时目录，按最终路径确定以便续传。"""
    final = Path(final_path).expanduser().resolve()
    digest = hashlib.sha256(
        str(final).encode("utf-8", "surrogatepass")
    ).hexdigest()[:16]
    # POSIX 的 /tmp 由多用户共享，按 uid 隔离；Windows 的 %TEMP% 本身按用户区分。
    uid = getattr(os, "getuid", lambda: None)()
    name = f"m3u8-cli-{uid}" if uid is not None else "m3u8-cli"
    return Path(tempfile.gettempdir()) / name / digest


def resolve_mp4_paths(output_arg):
    """mp4 最终文件保持用户指定路径，缓存放系统临时目录。"""
    final = Path(output_arg).expanduser().resolve()
    return final, temp_cache_dir(final)


def _length_units(text, windows):
    """按目标平台的文件名长度单位计数：Windows 为 UTF-16 单元，POSIX 为字节。"""
    if windows:
        return len(str(text).encode("utf-16-le")) // 2
    return len(os.fsencode(str(text)))


def fit_output_name(output, output_dir, windows=None):
    """截短输出名，使派生出的产物与缓存路径不超过平台限制。

    Windows 上标题类长文件名叠加输出目录后容易超过 MAX_PATH。这里同时检查：
    mp4 的最终文件与同目录 ``.partial`` 临时文件、hls 的
    ``<stem>/cache/segment_*.bin``，以及 mp4 系统临时缓存里的分片。
    其他平台只保证单个文件名不超过 NAME_LIMIT 字节。
    """
    if windows is None:
        windows = os.name == "nt"
    text = str(output)
    stem = Path(text).stem
    suffix = Path(text).suffix
    if not stem:
        return text

    def fits(candidate):
        final = Path(output_dir) / (candidate + suffix)
        if (
            _length_units(final.name, windows) > NAME_LIMIT
            or _length_units(
                candidate + PARTIAL_SUFFIX + suffix, windows
            )
            > NAME_LIMIT
        ):
            return False
        if not windows:
            return True
        paths = [
            final,
            final.parent / (
                candidate + PARTIAL_SUFFIX + suffix
            ),
            final.parent / candidate / CACHE_DIR / LONGEST_CACHE_FILE,
            temp_cache_dir(final) / LONGEST_CACHE_FILE,
        ]
        return all(
            _length_units(path, windows) <= WINDOWS_PATH_LIMIT
            for path in paths
        )

    if fits(stem):
        return text

    fitted = stem
    while fitted and not fits(fitted):
        fitted = fitted[:-1]
    fitted = fitted.rstrip(". ")
    if not fitted:
        # 极短的标题（例如全是点）截空后尝试固定回退名。
        fitted = "output" if fits("output") else ""
    if not fitted:
        raise HLSException(
            f"输出目录过长，无法创建任务文件：{output_dir}"
        )
    return fitted + suffix

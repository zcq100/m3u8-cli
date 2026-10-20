"""把已下载的 HLS 资源导出为本地目录。"""

import math
import re
import shutil
from pathlib import Path

from .errors import HLSException

SEGMENT_RE = re.compile(r"\d{6}\.(?:ts|m4s)")
KEY_RE = re.compile(r"key(?:_\d+)?\.bin")


def segment_suffix(playlist):
    """fMP4 用 .m4s，其余沿用 .ts。"""
    return ".m4s" if playlist["maps"] else ".ts"


def build_playlist(playlist, key_names):
    """生成引用本地文件的可播放媒体播放列表。"""
    suffix = segment_suffix(playlist)
    segments = playlist["segments"]
    durations = [float(s.get("duration") or 0) for s in segments]
    target = max(1, math.ceil(max(durations) if durations else 0))

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6" if playlist["maps"] else "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-MEDIA-SEQUENCE:{playlist['media_sequence']}",
    ]
    if playlist["maps"]:
        lines.append('#EXT-X-MAP:URI="init.mp4"')

    previous_key = None
    for i, segment in enumerate(segments):
        key = segment.get("key")
        if key is None and previous_key is not None:
            lines.append("#EXT-X-KEY:METHOD=NONE")
        elif key is not None and key != previous_key:
            attrs = [
                f'METHOD={key["method"]}',
                f'URI="{key_names[key["uri"]]}"',
            ]
            if key.get("iv"):
                attrs.append(f'IV={key["iv"]}')
            lines.append("#EXT-X-KEY:" + ",".join(attrs))
        previous_key = key
        lines.append(f"#EXTINF:{durations[i]:.3f},")
        lines.append(f"{i:06d}{suffix}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _clear_output(output_dir):
    """只删除本工具生成的播放列表、分片、初始化片段与密钥。"""
    for entry in output_dir.iterdir():
        if not (entry.is_file() or entry.is_symlink()):
            continue
        if (
            entry.name == "playlist.m3u8"
            or entry.name == "init.mp4"
            or SEGMENT_RE.fullmatch(entry.name)
            or KEY_RE.fullmatch(entry.name)
        ):
            entry.unlink()


def _targets(playlist, key_names):
    """返回本次导出会写入的所有目标路径。"""
    suffix = segment_suffix(playlist)
    targets = [f"{i:06d}{suffix}"
               for i in range(len(playlist["segments"]))]
    if playlist["maps"]:
        targets.append("init.mp4")
    targets.append("playlist.m3u8")
    targets.extend(key_names.values())
    return targets


def _check_targets(playlist, output_dir, key_names, overwrite):
    if output_dir.exists() and not output_dir.is_dir():
        raise HLSException(f"输出路径不是目录：{output_dir}")
    if len(playlist["maps"]) > 1:
        raise HLSException("不支持多个初始化片段")

    for name in _targets(playlist, key_names):
        target = output_dir / name
        if target.is_dir():
            raise HLSException(f"目标路径是目录：{target}")
        if not overwrite and target.exists():
            raise HLSException(
                f"目标已存在：{target}，使用 --overwrite 覆盖"
            )


def _check_sources(playlist, cache):
    for i in range(len(playlist["segments"])):
        source = cache / f"segment_{i:06d}.bin"
        if not source.is_file():
            raise HLSException(
                f"缓存分片缺失：{source}；请清理缓存后重试"
            )
    if playlist["maps"]:
        source = cache / "map_0000.bin"
        if not source.is_file():
            raise HLSException(
                f"缓存初始化片段缺失：{source}；请清理缓存后重试"
            )


def export_hls(playlist, cache, output_dir, key_data, key_names,
               overwrite):
    """把缓存分片、初始化片段与密钥导出到本地 HLS 目录。"""
    _check_targets(playlist, output_dir, key_names, overwrite)
    _check_sources(playlist, cache)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HLSException(
            f"无法创建输出目录：{output_dir}（{exc}）"
        ) from exc

    if overwrite:
        _clear_output(output_dir)

    suffix = segment_suffix(playlist)
    for i in range(len(playlist["segments"])):
        source = cache / f"segment_{i:06d}.bin"
        shutil.copyfile(source, output_dir / f"{i:06d}{suffix}")

    if playlist["maps"]:
        shutil.copyfile(
            cache / "map_0000.bin", output_dir / "init.mp4"
        )

    for uri, name in key_names.items():
        if uri not in key_data:
            raise HLSException(f"缺少密钥数据：{uri}")
        (output_dir / name).write_bytes(key_data[uri])

    part = output_dir / "playlist.m3u8.part"
    try:
        part.write_text(build_playlist(playlist, key_names), "utf-8")
        part.replace(output_dir / "playlist.m3u8")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return output_dir

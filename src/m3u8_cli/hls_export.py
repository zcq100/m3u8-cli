"""把已下载的 HLS 资源导出为本地目录。"""

import math
import re
import shutil
from pathlib import Path

from .errors import HLSException

SEGMENT_RE = re.compile(r"\d{6}\.(?:ts|m4s)")
KEY_RE = re.compile(r"key(?:_\d{3})?\.bin")


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


def export_hls(playlist, cache, output_dir, key_data, key_names,
               overwrite):
    """把缓存分片、初始化片段与密钥导出到本地 HLS 目录。"""
    playlist_path = output_dir / "playlist.m3u8"
    if not overwrite and playlist_path.exists():
        raise HLSException(
            f"输出文件已存在：{playlist_path}，使用 --overwrite 覆盖"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
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
        (output_dir / name).write_bytes(key_data[uri])

    part = output_dir / "playlist.m3u8.part"
    part.write_text(build_playlist(playlist, key_names), "utf-8")
    part.replace(playlist_path)
    return output_dir

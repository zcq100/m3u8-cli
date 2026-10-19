"""视频格式转换（FFmpeg）。"""

import shutil
import subprocess
from pathlib import Path

from .errors import HLSException

FORMAT_SUFFIX = {
    "hls": None,
    "mp4": ".mp4",
    "mkv": ".mkv",
    "webm": ".webm",
}

CODEC_ARGS = {
    "h264": ["-c:v", "libx264", "-c:a", "aac"],
    "hevc": ["-c:v", "libx265", "-c:a", "aac"],
    "copy": ["-c", "copy"],
}

WEBM_CODEC_ARGS = {
    # WebM 不支持 H.264/AAC，重编码统一使用 VP9/Opus。
    "h264": [
        "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
        "-c:a", "libopus",
    ],
    "hevc": [
        "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
        "-c:a", "libopus",
    ],
    "copy": ["-c", "copy"],
}


def resolve_output_dir(input_path, output_arg):
    """返回输出目录；未指定时使用输入文件 stem 作为目录名。"""
    if output_arg:
        output_dir = Path(output_arg).expanduser().resolve()
    else:
        output_dir = (Path.cwd() / input_path.stem).resolve()

    if output_dir.exists() and not output_dir.is_dir():
        raise HLSException(f"输出路径不是目录：{output_dir}")

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HLSException(f"无法创建输出目录：{output_dir}（{exc}）") from exc
    return output_dir


def codec_args(fmt, codec):
    """返回目标格式对应的 FFmpeg 编码参数。"""
    if fmt == "webm":
        return WEBM_CODEC_ARGS[codec]
    return CODEC_ARGS[codec]


def build_hls_command(input_path, output_dir, codec, segment_time):
    """构造 HLS 转换的 FFmpeg 命令。"""
    command = [
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(input_path),
        "-map", "0:v?", "-map", "0:a?",
        *codec_args("hls", codec),
    ]

    if codec != "copy":
        command += [
            "-force_key_frames",
            f"expr:gte(t,n_forced*{segment_time})",
        ]

    command += [
        "-f", "hls",
        "-hls_time", str(segment_time),
        "-hls_playlist_type", "vod",
        "-hls_list_size", "0",
        "-hls_base_url", "ts/",
        "-hls_segment_filename", str(output_dir / "ts" / "%06d.ts"),
        str(output_dir / "playlist.m3u8"),
    ]
    return command


def build_file_command(input_path, fmt, codec, partial):
    """构造单文件格式转换的 FFmpeg 命令。"""
    command = [
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(input_path),
        "-map", "0:v?", "-map", "0:a?",
        *codec_args(fmt, codec),
    ]

    if fmt == "mp4":
        command += ["-movflags", "+faststart"]
        if codec == "hevc":
            command += ["-tag:v", "hvc1"]

    command.append(str(partial))
    return command


def convert_video(input_arg, fmt, codec, output_arg,
                  segment_time, overwrite):
    """转换视频并返回最终产物路径。"""
    input_path = Path(input_arg).expanduser().resolve()
    if not input_path.is_file():
        raise HLSException(f"输入文件不存在：{input_path}")

    output_dir = resolve_output_dir(input_path, output_arg)

    if fmt == "hls":
        playlist = output_dir / "playlist.m3u8"
        segments_dir = output_dir / "ts"

        if playlist == input_path:
            raise HLSException(f"输出文件与输入文件相同：{playlist}")
        if segments_dir.exists() and not segments_dir.is_dir():
            raise HLSException(f"分片路径不是目录：{segments_dir}")
        if not overwrite and (playlist.exists() or segments_dir.exists()):
            raise HLSException(
                f"目标已存在：{output_dir}，使用 --overwrite 覆盖"
            )

        if overwrite:
            playlist.unlink(missing_ok=True)
            if segments_dir.exists():
                shutil.rmtree(segments_dir)
        segments_dir.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            build_hls_command(input_path, output_dir, codec, segment_time),
            check=True,
        )
        return playlist

    output = output_dir / (input_path.stem + FORMAT_SUFFIX[fmt])
    if output == input_path:
        raise HLSException(f"输出文件与输入文件相同：{output}")
    if output.exists() and not overwrite:
        raise HLSException(
            f"输出文件已存在：{output}，使用 --overwrite 覆盖"
        )

    partial = output_dir / (
        input_path.stem + ".partial" + FORMAT_SUFFIX[fmt]
    )
    try:
        subprocess.run(
            build_file_command(input_path, fmt, codec, partial),
            check=True,
        )
        partial.replace(output)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return output

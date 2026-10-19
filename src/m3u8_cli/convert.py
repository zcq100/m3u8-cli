"""视频格式转换（FFmpeg）。"""

import secrets
import subprocess
from pathlib import Path

from .errors import HLSException

HLS_KEY_NAME = "key.bin"
HLS_KEY_INFO_NAME = ".key_info.txt"
HLS_KEY_SIZE = 16

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


def build_hls_command(input_path, output_dir, codec, segment_time,
                      encrypt=False):
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
    ]

    if encrypt:
        command += [
            "-hls_key_info_file",
            str(output_dir / HLS_KEY_INFO_NAME),
        ]

    command += [
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


def _clear_hls_output(playlist, segments_dir):
    """只清理旧播放列表与 6 位编号分片，保留 ts/ 中的其他文件。"""
    playlist.unlink(missing_ok=True)
    if not segments_dir.is_dir():
        return
    for entry in segments_dir.iterdir():
        stem = entry.name[:-3]
        if (
            entry.name.endswith(".ts")
            and len(stem) == 6
            and stem.isascii()
            and stem.isdigit()
            and (entry.is_file() or entry.is_symlink())
        ):
            entry.unlink()


def _write_hls_key(output_dir):
    """生成新的 AES-128 密钥并返回其路径。"""
    key_path = output_dir / HLS_KEY_NAME
    if key_path.is_symlink():
        key_path.unlink()
    elif key_path.exists() and key_path.is_dir():
        raise HLSException(f"密钥路径是目录：{key_path}")
    key_path.write_bytes(secrets.token_bytes(HLS_KEY_SIZE))
    return key_path


def _write_key_info(output_dir, key_path):
    """写入 FFmpeg 的 key-info 临时文件并返回其路径。"""
    key_info = output_dir / HLS_KEY_INFO_NAME
    if key_info.is_symlink():
        key_info.unlink()
    elif key_info.exists() and key_info.is_dir():
        raise HLSException(f"密钥信息路径是目录：{key_info}")
    key_info.write_text(
        f"{HLS_KEY_NAME}\n{key_path}\n",
        encoding="utf-8",
    )
    return key_info


def convert_video(input_arg, fmt, codec, output_arg,
                  segment_time, overwrite, encrypt=False):
    """转换视频并返回最终产物路径。"""
    if encrypt and fmt != "hls":
        raise HLSException("--encrypt 仅适用于 HLS")

    input_path = Path(input_arg).expanduser().resolve()
    if not input_path.is_file():
        raise HLSException(f"输入文件不存在：{input_path}")

    output_dir = resolve_output_dir(input_path, output_arg)

    if fmt == "hls":
        playlist = output_dir / "playlist.m3u8"
        segments_dir = output_dir / "ts"
        key_path = output_dir / HLS_KEY_NAME

        if playlist == input_path:
            raise HLSException(f"输出文件与输入文件相同：{playlist}")
        if segments_dir == input_path or segments_dir in input_path.parents:
            raise HLSException(f"输入文件位于分片目录中：{input_path}")
        if segments_dir.is_symlink():
            raise HLSException(f"分片路径是符号链接：{segments_dir}")
        if segments_dir.exists() and not segments_dir.is_dir():
            raise HLSException(f"分片路径不是目录：{segments_dir}")
        if playlist.is_dir():
            raise HLSException(f"播放列表路径不是文件：{playlist}")
        if not overwrite and (
            playlist.exists()
            or playlist.is_symlink()
            or segments_dir.exists()
            or (encrypt and (key_path.exists() or key_path.is_symlink()))
        ):
            raise HLSException(
                f"目标已存在：{output_dir}，使用 --overwrite 覆盖"
            )

        if overwrite:
            _clear_hls_output(playlist, segments_dir)
        segments_dir.mkdir(parents=True, exist_ok=True)

        key_info = None
        if encrypt:
            key_path = _write_hls_key(output_dir)
            try:
                key_info = _write_key_info(output_dir, key_path)
            except Exception:
                key_path.unlink(missing_ok=True)
                raise
        else:
            key_path = None

        try:
            subprocess.run(
                build_hls_command(
                    input_path, output_dir, codec, segment_time, encrypt
                ),
                check=True,
            )
        except Exception:
            if key_info is not None:
                key_info.unlink(missing_ok=True)
            if key_path is not None:
                key_path.unlink(missing_ok=True)
            raise

        if key_info is not None:
            key_info.unlink(missing_ok=True)
        return playlist

    output = output_dir / (input_path.stem + FORMAT_SUFFIX[fmt])
    if output == input_path:
        raise HLSException(f"输出文件与输入文件相同：{output}")
    if output.is_dir():
        raise HLSException(f"输出路径是目录：{output}")
    if not overwrite and (output.exists() or output.is_symlink()):
        raise HLSException(
            f"输出文件已存在：{output}，使用 --overwrite 覆盖"
        )

    partial = output_dir / (
        input_path.stem + ".partial" + FORMAT_SUFFIX[fmt]
    )
    if partial.is_symlink():
        partial.unlink()
    elif partial.exists():
        if partial.is_dir():
            raise HLSException(f"临时输出路径是目录：{partial}")
        partial.unlink()

    try:
        subprocess.run(
            build_file_command(input_path, fmt, codec, partial),
            check=True,
        )
        partial.replace(output)
    except Exception:
        if partial.is_file() or partial.is_symlink():
            partial.unlink()
        raise
    return output

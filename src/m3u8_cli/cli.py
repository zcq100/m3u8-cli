"""命令行入口与下载流程编排。"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import requests

from .convert import convert_video
from .download import DownloadOptions, download_hls
from .errors import HLSException


def build_parser():
    parser = argparse.ArgumentParser(
        prog="m3u8-cli",
        description="HLS 下载与视频格式转换工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser(
        "download",
        help="下载 HLS（m3u8）视频",
        description="并发下载 HLS 分片，产物保存在任务子目录中",
    )
    download.add_argument("url", help="m3u8 地址")
    download.add_argument(
        "-f", "--format", choices=("mp4", "hls"), default="mp4",
        help="输出格式：mp4（合并封装，默认）或 hls（保留分片，不合并）",
    )
    download.add_argument(
        "-o", "--output",
        help="mp4: 最终文件名；hls: 任务目录（默认 output.mp4 / output）",
    )
    download.add_argument("-j", "--jobs", type=int, default=8)
    download.add_argument("--retries", type=int, default=3)
    download.add_argument("--timeout", type=int, default=30)
    download.add_argument("--referer")
    download.add_argument("--cookie")
    download.add_argument("--user-agent", default="Mozilla/5.0")
    download.add_argument(
        "--overwrite", action="store_true",
        help="覆盖已有输出文件",
    )

    convert = subparsers.add_parser(
        "convert",
        help="把本地视频转换为 HLS 或其他格式",
        description="用 FFmpeg 转换视频格式，默认输出 HLS",
    )
    convert.add_argument("input", help="输入视频文件")
    convert.add_argument(
        "-f", "--format", choices=("hls", "mp4", "mkv", "webm"),
        default="hls", help="输出格式（默认 hls）",
    )
    convert.add_argument(
        "-c", "--codec", choices=("h264", "hevc", "copy"),
        default="h264", help="编码方式（默认 h264）",
    )
    convert.add_argument(
        "-o", "--output-dir",
        help="输出目录；默认为输入文件名",
    )
    convert.add_argument(
        "--segment-time", type=int, default=6,
        help="HLS 分片时长（秒，默认 6）",
    )
    convert.add_argument(
        "--overwrite", action="store_true",
        help="覆盖已有产物",
    )
    convert.add_argument(
        "--encrypt", action="store_true",
        help="为 HLS 分片启用 AES-128 加密",
    )
    return parser


def run_download(args):
    if args.format == "mp4" and not shutil.which("ffmpeg"):
        sys.exit("请先安装 ffmpeg")

    output_arg = args.output or (
        "output" if args.format == "hls" else "output.mp4"
    )

    headers = {"User-Agent": args.user_agent}
    if args.referer:
        headers["Referer"] = args.referer
    if args.cookie:
        headers["Cookie"] = args.cookie

    def on_progress(completed, total):
        print(f"\r下载进度：{completed}/{total}", end="", flush=True)

    output = download_hls(
        DownloadOptions(
            url=args.url,
            output=Path(output_arg),
            format=args.format,
            jobs=args.jobs,
            retries=args.retries,
            timeout=args.timeout,
            headers=headers,
            overwrite=args.overwrite,
        ),
        on_progress=on_progress,
    )
    print()
    print(f"完成：{output}")


def run_convert(args):
    if not shutil.which("ffmpeg"):
        sys.exit("请先安装 ffmpeg")

    output = convert_video(
        args.input,
        args.format,
        args.codec,
        args.output_dir,
        args.segment_time,
        args.overwrite,
        args.encrypt,
    )
    print(f"完成：{output}")


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "download":
        if args.jobs < 1 or args.timeout < 1 or args.retries < 0:
            parser.error("jobs/timeout 必须为正数，retries 不能为负数")
        handler = run_download
    elif args.command == "convert":
        if args.encrypt and args.format != "hls":
            parser.error("--encrypt 仅适用于 HLS")
        if args.format == "hls" and args.segment_time < 1:
            parser.error("segment-time 必须为正整数")
        handler = run_convert
    else:
        parser.error(f"未知子命令：{args.command}")

    try:
        handler(args)
    except (HLSException, OSError, requests.RequestException,
            subprocess.CalledProcessError) as exc:
        sys.exit(f"\n错误：{exc}")

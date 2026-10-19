"""命令行入口与下载流程编排。"""

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import sys

import requests

from .convert import convert_video
from .downloader import Downloader
from .errors import HLSException
from .merge import combine_segments, mux
from .paths import resolve_task_paths
from .playlist import load_playlist


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
        "-o", "--output", default="output.mp4",
        help="最终文件名；产物保存在以文件 stem 命名的任务目录中",
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
    if not shutil.which("ffmpeg"):
        sys.exit("请先安装 ffmpeg")

    output, cache = resolve_task_paths(args.output)

    if output.exists() and not args.overwrite:
        sys.exit(f"输出文件已存在：{output}，使用 --overwrite 覆盖")

    cache.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": args.user_agent}
    if args.referer:
        headers["Referer"] = args.referer
    if args.cookie:
        headers["Cookie"] = args.cookie

    print("读取 m3u8...")
    playlist = load_playlist(args.url, headers, args.timeout)
    segments = playlist["segments"]
    fingerprint = playlist["fingerprint"]

    state_file = cache / "state.json"
    previous = None
    if state_file.exists():
        try:
            previous = json.loads(state_file.read_text("utf-8"))
        except (OSError, ValueError):
            previous = None

    if previous and previous.get("fingerprint") != fingerprint:
        print("播放列表已变化，清除旧分片缓存")
        for p in cache.glob("segment_*.bin"):
            p.unlink(missing_ok=True)
        for p in cache.glob("map_*.bin"):
            p.unlink(missing_ok=True)

    state_file.write_text(
        json.dumps({"fingerprint": fingerprint}, indent=2),
        encoding="utf-8",
    )

    downloader = Downloader(headers, args.timeout, args.retries)

    # 初始化片段也在合并前下载完成。
    map_paths = {}
    for i, map_url in enumerate(playlist["maps"]):
        path = cache / f"map_{i:04d}.bin"
        map_paths[map_url] = downloader.download(map_url, path)
        print(f"初始化片段已准备：{i + 1}/{len(playlist['maps'])}")

    jobs = []
    for i, segment in enumerate(segments):
        path = cache / f"segment_{i:06d}.bin"
        jobs.append((i, segment, path))

    print(f"共 {len(jobs)} 个分片，并发数 {args.jobs}")
    completed = 0

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.jobs
    ) as pool:
        futures = {
            pool.submit(
                downloader.download,
                segment["url"],
                path,
                segment["key"],
                segment["sequence"],
            ): (i, segment, path)
            for i, segment, path in jobs
        }

        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
                completed += 1
                print(
                    f"\r下载进度：{completed}/{len(jobs)}",
                    end="", flush=True,
                )
        except Exception:
            for future in futures:
                future.cancel()
            raise

    print("\n所有分片下载完成，开始本地合并")

    combined = combine_segments(
        segments, playlist["maps"], map_paths, cache
    )
    mux(combined, cache, output)

    print(f"完成：{output}")
    print(f"分片缓存：{cache}")


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

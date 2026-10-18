"""命令行入口与下载流程编排。"""

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import sys

import requests

from .downloader import Downloader
from .errors import HLSException
from .merge import combine_segments, mux
from .paths import resolve_task_paths
from .playlist import load_playlist


def build_parser():
    parser = argparse.ArgumentParser(
        prog="download-v",
        description="并发下载 HLS 分片，产物保存在任务子目录中",
    )
    parser.add_argument("url", help="m3u8 地址")
    parser.add_argument(
        "-o", "--output", default="output.mp4",
        help="最终文件名；产物保存在以文件 stem 命名的任务目录中",
    )
    parser.add_argument("-j", "--jobs", type=int, default=8)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--referer")
    parser.add_argument("--cookie")
    parser.add_argument("--user-agent", default="Mozilla/5.0")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="覆盖已有输出文件"
    )
    return parser


def run(args):
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


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.jobs < 1 or args.timeout < 1 or args.retries < 0:
        parser.error("jobs/timeout 必须为正数，retries 不能为负数")

    try:
        run(args)
    except (HLSException, requests.RequestException,
            subprocess.CalledProcessError) as exc:
        sys.exit(f"\n错误：{exc}")

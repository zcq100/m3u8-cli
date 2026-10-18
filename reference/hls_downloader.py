
#!/usr/bin/env python3
"""使用 FFmpeg 下载并无损封装有权访问的 HLS 视频。"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="HLS 下载器：支持常见 AES-128、TS、fMP4 和 MP4 封装"
    )
    parser.add_argument("url", help="m3u8 播放列表 URL")
    parser.add_argument("-o", "--output", default="output.mp4")
    parser.add_argument("--referer", help="可选 Referer")
    parser.add_argument("--user-agent", default="Mozilla/5.0")
    parser.add_argument("--cookie", help="可选 Cookie")
    parser.add_argument("--timeout", type=int, default=30,
                        help="网络超时秒数，默认 30")
    parser.add_argument("--retries", type=int, default=5,
                        help="网络重连次数，默认 5")
    parser.add_argument("--overwrite", action="store_true",
                        help="允许覆盖输出文件")
    parser.add_argument("--reencode", action="store_true",
                        help="重新编码为 H.264/AAC，兼容性更好")
    args = parser.parse_args()

    if not shutil.which("ffmpeg"):
        sys.exit("错误：未找到 ffmpeg，请先安装。")

    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    if output.exists() and not args.overwrite:
        sys.exit(f"文件已存在：{output}；如需覆盖，请添加 --overwrite")

    headers = {
        "User-Agent": args.user_agent,
    }
    if args.referer:
        headers["Referer"] = args.referer
    if args.cookie:
        headers["Cookie"] = args.cookie

    # FFmpeg 的 HTTP headers 使用 CRLF 分隔。
    header_text = "".join(
        f"{name}: {value}\r\n" for name, value in headers.items()
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y" if args.overwrite else "-n",
        "-rw_timeout", str(args.timeout * 1_000_000),
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-headers", header_text,
        "-i", args.url,
        "-map", "0:v?",
        "-map", "0:a?",
        "-map_metadata", "0",
    ]

    if args.reencode:
        command += [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-c:a", "aac",
            "-b:a", "192k",
        ]
    else:
        # 不重新编码，尽可能保持原始画质。
        command += ["-c", "copy"]

    command += [
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "2048",
        str(output),
    ]

    print(f"输入：{args.url}")
    print(f"输出：{output.resolve()}")
    print("开始解析、下载、解密（如适用）并封装 HLS...")

    last_error = None

    for attempt in range(args.retries + 1):
        try:
            subprocess.run(command, check=True)
            print(f"\n完成：{output.resolve()}")
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            print(
                f"\nFFmpeg 执行失败，"
                f"尝试 {attempt + 1}/{args.retries + 1}",
                file=sys.stderr,
            )
            if attempt < args.retries:
                # 删除失败的输出，避免重试时混淆。
                if output.exists():
                    output.unlink()
        except KeyboardInterrupt:
            print("\n用户取消。", file=sys.stderr)
            sys.exit(130)

    sys.exit(
        f"下载失败，已重试 {args.retries} 次。"
        f"最后错误码：{last_error.returncode if last_error else '未知'}"
    )


if __name__ == "__main__":
    main()


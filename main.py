
#!/usr/bin/env python3
"""并发 HLS 下载器：本地缓存、AES-128 解密和 FFmpeg 封装。"""

import argparse
import concurrent.futures
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


class HLSException(RuntimeError):
    pass


def parse_attrs(line):
    """解析 EXT-X-KEY 等标签中的属性，支持引号内逗号。"""
    result = {}
    pattern = r'([A-Z0-9-]+)=("(?:[^"\\]|\\.)*"|[^,]*)'
    for key, value in re.findall(pattern, line):
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        result[key] = value
    return result


def request_text(url, headers, timeout):
    with requests.get(
        url, headers=headers, timeout=timeout
    ) as response:
        response.raise_for_status()
        return response.text


def load_playlist(url, headers, timeout, depth=0):
    """解析媒体播放列表；如果是 master playlist，选择最高带宽变体。"""
    if depth > 5:
        raise HLSException("主播放列表嵌套层数过多")

    text = request_text(url, headers, timeout)
    lines = [x.strip() for x in text.splitlines() if x.strip()]

    if not lines or lines[0] != "#EXTM3U":
        raise HLSException("服务器返回的内容不是有效的 M3U8")

    variants = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_attrs(line)
            for following in lines[i + 1:]:
                if not following.startswith("#"):
                    try:
                        bandwidth = int(attrs.get("BANDWIDTH", "0"))
                    except ValueError:
                        bandwidth = 0
                    variants.append(
                        (bandwidth, urljoin(url, following))
                    )
                    break

    if variants:
        variants.sort(reverse=True)
        print(f"发现 {len(variants)} 个清晰度，选择最高带宽变体")
        return load_playlist(
            variants[0][1], headers, timeout, depth + 1
        )

    sequence = 0
    key = None
    current_map = None
    segments = []
    maps = []
    pending_map = None

    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1])
            except ValueError as exc:
                raise HLSException("无效的 MEDIA-SEQUENCE") from exc

        elif line.startswith("#EXT-X-KEY:"):
            attrs = parse_attrs(line)
            method = attrs.get("METHOD", "")

            if method == "NONE":
                key = None
            elif method == "AES-128":
                keyformat = attrs.get("KEYFORMAT", "identity")
                if keyformat != "identity":
                    raise HLSException(
                        f"不支持的 KEYFORMAT：{keyformat}"
                    )
                if not attrs.get("URI"):
                    raise HLSException("AES-128 标签缺少密钥 URI")
                key = {
                    "method": method,
                    "uri": urljoin(url, attrs["URI"]),
                    "iv": attrs.get("IV"),
                }
            else:
                raise HLSException(
                    f"不支持的加密方式：{method}；不处理 DRM"
                )

        elif line.startswith("#EXT-X-MAP:"):
            attrs = parse_attrs(line)
            if not attrs.get("URI"):
                raise HLSException("EXT-X-MAP 缺少 URI")
            if "BYTERANGE" in attrs:
                raise HLSException("暂不支持初始化片段 BYTERANGE")
            pending_map = urljoin(url, attrs["URI"])

        elif line.startswith("#EXT-X-BYTERANGE:"):
            raise HLSException(
                "暂不支持 EXT-X-BYTERANGE，请使用完整分片 URL"
            )

        elif not line.startswith("#"):
            segment_key = dict(key) if key else None
            segment_map = pending_map or current_map

            if pending_map:
                current_map = pending_map
                pending_map = None

            segments.append({
                "url": urljoin(url, line),
                "sequence": sequence + len(segments),
                "key": segment_key,
                "map": segment_map,
            })

    if not segments:
        raise HLSException("播放列表中没有媒体分片")

    unique_maps = list(dict.fromkeys(
        s["map"] for s in segments if s["map"]
    ))
    if len(unique_maps) > 1:
        raise HLSException("检测到多个不同的初始化片段，当前版本不支持")

    if unique_maps:
        maps = unique_maps

    return {
        "playlist_url": url,
        "segments": segments,
        "maps": maps,
        "fingerprint": hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest(),
    }


class Downloader:
    def __init__(self, headers, timeout, retries):
        self.headers = headers
        self.timeout = timeout
        self.retries = retries
        self.local = threading.local()
        self.keys = {}
        self.key_lock = threading.Lock()

    def session(self):
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers.update(self.headers)
        return self.local.session

    def get_bytes(self, url):
        response = self.session().get(
            url, timeout=self.timeout
        )
        response.raise_for_status()
        return response.content

    def get_key(self, key_url):
        # 同一密钥只请求一次，避免并发重复拉取。
        with self.key_lock:
            if key_url in self.keys:
                return self.keys[key_url]

            data = self.get_bytes(key_url)
            if len(data) != 16:
                raise HLSException(
                    f"AES-128 密钥长度异常：{len(data)} 字节"
                )

            self.keys[key_url] = data
            return data

    def decrypt(self, data, key_info, sequence):
        if not key_info:
            return data

        key = self.get_key(key_info["uri"])
        iv_text = key_info.get("iv")

        if iv_text:
            if not re.fullmatch(r"0[xX][0-9a-fA-F]{1,32}", iv_text):
                raise HLSException("无效的 AES IV")
            iv = bytes.fromhex(iv_text[2:].zfill(32))
        else:
            # HLS AES-128 未指定 IV 时使用媒体序列号。
            iv = sequence.to_bytes(16, "big")

        if not data or len(data) % AES.block_size:
            raise HLSException("AES-128 密文长度不符合 CBC 块要求")

        cipher = AES.new(key, AES.MODE_CBC, iv)
        plaintext = cipher.decrypt(data)

        try:
            return unpad(plaintext, AES.block_size)
        except ValueError as exc:
            raise HLSException(
                "AES 解密后的填充无效，请检查密钥、IV 和内容类型"
            ) from exc

    def download(self, url, path, key_info=None, sequence=0):
        if path.exists() and path.stat().st_size > 0:
            return path

        last_error = None

        for attempt in range(self.retries + 1):
            try:
                data = self.get_bytes(url)
                data = self.decrypt(data, key_info, sequence)

                temp = path.with_suffix(path.suffix + ".part")
                temp.write_bytes(data)
                temp.replace(path)
                return path

            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 10))

        raise HLSException(
            f"下载失败：{url}\n最后错误：{last_error}"
        )


def resolve_task_paths(output_arg):
    """返回任务目录内的最终文件路径和缓存目录。"""
    output = Path(output_arg).expanduser().resolve()

    # -o output/name.mp4 时直接复用 output/，避免嵌套成 output/output/。
    if output.parent.name == output.stem:
        task_dir = output.parent
    else:
        task_dir = output.parent / output.stem

    if task_dir.exists() and not task_dir.is_dir():
        raise HLSException(f"任务目录路径不是目录：{task_dir}")

    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir / output.name, task_dir / "cache"


def main():
    parser = argparse.ArgumentParser(
        description="并发下载 HLS 分片，产物保存在任务子目录中"
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
    args = parser.parse_args()

    if args.jobs < 1 or args.timeout < 1 or args.retries < 0:
        parser.error("jobs/timeout 必须为正数，retries 不能为负数")

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

    # HLS AES-128 的分片在这里已经解密。
    # TS 直接拼接；fMP4 先写初始化片段，再拼接媒体片段。
    is_fmp4 = bool(playlist["maps"])
    combined = cache / ("combined.mp4" if is_fmp4 else "combined.ts")

    with combined.open("wb") as out:
        if is_fmp4:
            out.write(map_paths[playlist["maps"][0]].read_bytes())

        for i, segment in enumerate(segments):
            path = cache / f"segment_{i:06d}.bin"
            with path.open("rb") as src:
                shutil.copyfileobj(src, out, length=1024 * 1024)

    # 只对本地合并文件运行 FFmpeg，不再访问远程分片。
    # 临时文件与最终文件同属一个任务目录，重命名保持原子性。
    temp_output = cache / (output.stem + ".partial" + output.suffix)
    command = [
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(combined),
        "-map", "0",
        "-c", "copy",
        "-movflags", "+faststart",
        str(temp_output),
    ]

    try:
        subprocess.run(command, check=True)
        temp_output.replace(output)
    except Exception:
        temp_output.unlink(missing_ok=True)
        raise

    print(f"完成：{output}")
    print(f"分片缓存：{cache}")


if __name__ == "__main__":
    try:
        main()
    except (HLSException, requests.RequestException,
            subprocess.CalledProcessError) as exc:
        sys.exit(f"\n错误：{exc}")


"""可复用的 HLS 下载流程。"""

import concurrent.futures
import json
from dataclasses import dataclass, field
from pathlib import Path

from .downloader import Downloader
from .errors import DownloadCancelled, HLSException
from .hls_export import export_hls
from .merge import combine_segments, mux
from .paths import resolve_task_paths
from .playlist import load_playlist


@dataclass
class DownloadOptions:
    url: str
    output: Path
    format: str = "mp4"
    jobs: int = 8
    retries: int = 3
    timeout: int = 30
    headers: dict = field(default_factory=dict)
    overwrite: bool = False


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("下载已取消")


def _resolve_paths(options):
    if options.format == "hls":
        task_dir = Path(options.output).expanduser().resolve()
        if task_dir.exists() and not task_dir.is_dir():
            raise HLSException(f"任务目录不是目录：{task_dir}")
        return task_dir / "playlist.m3u8", task_dir / "cache"
    return resolve_task_paths(options.output)


def _load_state(state_file):
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def download_hls(options, on_progress=None, cancel=None):
    """下载 HLS 并输出；mp4 返回最终文件，hls 返回任务目录。"""
    if options.format not in ("mp4", "hls"):
        raise HLSException(f"不支持的输出格式：{options.format}")

    _check_cancel(cancel)
    final, cache = _resolve_paths(options)
    task_dir = cache.parent

    if final.exists() and not options.overwrite:
        raise HLSException(
            f"输出文件已存在：{final}，使用 --overwrite 覆盖"
        )

    cache.mkdir(parents=True, exist_ok=True)

    print("读取 m3u8...")
    playlist = load_playlist(options.url, options.headers, options.timeout)
    segments = playlist["segments"]
    fingerprint = playlist["fingerprint"]

    state_file = cache / "state.json"
    previous = _load_state(state_file)
    if previous and (
        previous.get("fingerprint") != fingerprint
        or previous.get("mode", "mp4") != options.format
    ):
        print("播放列表或输出模式已变化，清除旧分片缓存")
        for p in cache.glob("segment_*.bin"):
            p.unlink(missing_ok=True)
        for p in cache.glob("map_*.bin"):
            p.unlink(missing_ok=True)

    state_file.write_text(
        json.dumps(
            {"fingerprint": fingerprint, "mode": options.format},
            indent=2,
        ),
        encoding="utf-8",
    )

    downloader = Downloader(
        options.headers, options.timeout, options.retries
    )

    key_data = {}
    key_names = {}
    if options.format == "hls":
        key_uris = list(dict.fromkeys(
            s["key"]["uri"] for s in segments if s.get("key")
        ))
        for uri in key_uris:
            _check_cancel(cancel)
            key_data[uri] = downloader.get_key(uri)
        for index, uri in enumerate(key_uris):
            key_names[uri] = (
                "key.bin" if len(key_uris) == 1
                else f"key_{index:03d}.bin"
            )

    map_paths = {}
    for i, map_url in enumerate(playlist["maps"]):
        _check_cancel(cancel)
        path = cache / f"map_{i:04d}.bin"
        map_paths[map_url] = downloader.download(
            map_url, path, decrypt=False
        )
        print(f"初始化片段已准备：{i + 1}/{len(playlist['maps'])}")

    jobs = [
        (i, segment, cache / f"segment_{i:06d}.bin")
        for i, segment in enumerate(segments)
    ]
    print(f"共 {len(jobs)} 个分片，并发数 {options.jobs}")

    completed = 0
    decrypt = options.format == "mp4"

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=options.jobs
    ) as pool:
        pending = set()
        iterator = iter(jobs)

        def submit_next():
            try:
                i, segment, path = next(iterator)
            except StopIteration:
                return False
            future = pool.submit(
                downloader.download,
                segment["url"],
                path,
                segment["key"],
                segment["sequence"],
                decrypt,
            )
            pending.add(future)
            return True

        try:
            _check_cancel(cancel)
            for _ in range(options.jobs * 2):
                if not submit_next():
                    break

            while pending:
                done, _ = concurrent.futures.wait(
                    pending,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    pending.discard(future)
                    future.result()
                    completed += 1
                    if on_progress is not None:
                        on_progress(completed, len(jobs))
                _check_cancel(cancel)
                for _ in range(len(done)):
                    if not submit_next():
                        break
        except BaseException:
            for future in pending:
                future.cancel()
            raise

    if options.format == "hls":
        _check_cancel(cancel)
        return export_hls(
            playlist,
            cache,
            task_dir,
            key_data,
            key_names,
            options.overwrite,
        )

    print("\n所有分片下载完成，开始本地合并")
    combined = combine_segments(
        segments, playlist["maps"], map_paths, cache
    )
    mux(combined, cache, final)
    return final

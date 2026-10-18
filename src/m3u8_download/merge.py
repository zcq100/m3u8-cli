"""本地分片合并与 FFmpeg 封装。"""

import shutil
import subprocess


def combine_segments(segments, maps, map_paths, cache):
    """按播放列表顺序把已解密分片写入 combined 文件。"""
    is_fmp4 = bool(maps)
    combined = cache / ("combined.mp4" if is_fmp4 else "combined.ts")

    with combined.open("wb") as out:
        # fMP4 先写初始化片段，再拼接媒体分片。
        if is_fmp4:
            out.write(map_paths[maps[0]].read_bytes())

        for i in range(len(segments)):
            path = cache / f"segment_{i:06d}.bin"
            with path.open("rb") as src:
                shutil.copyfileobj(src, out, length=1024 * 1024)

    return combined


def mux(combined, cache, output):
    """把合并文件无损封装为最终 MP4，成功后原子替换。"""
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

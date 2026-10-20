"""m3u8 播放列表获取与解析。"""

import hashlib
import math
import re
from urllib.parse import urljoin

import requests

from .errors import HLSException


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


def request_text(url, headers, timeout, verify=True):
    with requests.get(
        url, headers=headers, timeout=timeout, verify=verify
    ) as response:
        response.raise_for_status()
        return response.text


def load_playlist(url, headers, timeout, verify=True, depth=0):
    """解析媒体播放列表；如果是 master playlist，选择最高带宽变体。"""
    if depth > 5:
        raise HLSException("主播放列表嵌套层数过多")

    text = request_text(url, headers, timeout, verify=verify)
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
            variants[0][1], headers, timeout, verify=verify,
            depth=depth + 1,
        )

    media_sequence = 0
    key = None
    current_map = None
    segments = []
    maps = []
    pending_map = None
    pending_duration = 0.0

    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except ValueError as exc:
                raise HLSException("无效的 MEDIA-SEQUENCE") from exc

        elif line.startswith("#EXTINF:"):
            try:
                pending_duration = float(
                    line.split(":", 1)[1].split(",", 1)[0]
                )
            except ValueError as exc:
                raise HLSException("无效的 EXTINF") from exc
            if not math.isfinite(pending_duration):
                raise HLSException("无效的 EXTINF")

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
                "sequence": media_sequence + len(segments),
                "duration": pending_duration,
                "key": segment_key,
                "map": segment_map,
            })
            pending_duration = 0.0

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
        "media_sequence": media_sequence,
        "fingerprint": hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest(),
    }

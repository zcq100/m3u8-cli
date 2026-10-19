"""分片下载、密钥缓存与 AES-128 解密。"""

import re
import threading
import time

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .errors import HLSException


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

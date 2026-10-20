/*
 * m3u8-cli 链接检测器：background / content / popup 共用的纯函数。
 * 以经典脚本方式加载（无构建），同时兼容 Node 的 require。
 */
(function (root) {
  "use strict";

  const HLS_CONTENT_TYPE =
    /(?:application|audio)\/(?:vnd\.apple\.|x-)?mpegurl/i;
  const OUTPUT_LIMIT = 200;
  const TITLE_LIMIT = 300;
  const RESERVED_NAMES = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$/i;

  function looksLikePlaylistUrl(raw) {
    try {
      const url = new URL(raw);
      if (url.protocol !== "http:" && url.protocol !== "https:") {
        return false;
      }
      const path = url.pathname.toLowerCase();
      return path.endsWith(".m3u8") || path.endsWith(".m3u");
    } catch (_) {
      return false;
    }
  }

  function isHlsContentType(value) {
    return HLS_CONTENT_TYPE.test(String(value || ""));
  }

  function normalizeCandidateUrl(raw, base) {
    if (typeof raw !== "string") {
      return null;
    }
    const trimmed = raw.trim();
    if (!trimmed) {
      return null;
    }
    try {
      const url = base ? new URL(trimmed, base) : new URL(trimmed);
      if (url.protocol !== "http:" && url.protocol !== "https:") {
        return null;
      }
      return url.href;
    } catch (_) {
      return null;
    }
  }

  function stripControlCharacters(value) {
    return Array.from(String(value || ""))
      .filter((ch) => {
        const code = ch.charCodeAt(0);
        return code >= 32 && code !== 127;
      })
      .join("");
  }

  /** 页面标题的轻量清洗：去掉控制字符、合并空白、限制长度。 */
  function cleanPageTitle(raw) {
    if (typeof raw !== "string") {
      return "";
    }
    const title = stripControlCharacters(raw.normalize("NFC"))
      .replace(/\s+/g, " ")
      .trim();
    return title.length > TITLE_LIMIT
      ? title.slice(0, TITLE_LIMIT).trim()
      : title;
  }

  function sanitizeOutputName(raw, format) {
    const hls = format === "hls";
    const limit = hls ? OUTPUT_LIMIT : OUTPUT_LIMIT - ".mp4".length;
    let name = stripControlCharacters(String(raw || "").normalize("NFC"));
    name = name.replace(/\.m3u8?$/i, "");
    name = name.replace(/[\\/:*?"<>|]/g, "_");
    name = name.replace(/\s+/g, " ").trim();
    name = name.replace(/^[.\s]+/, "").replace(/[.\s]+$/, "");
    if (!name) {
      name = "output";
    }
    if (RESERVED_NAMES.test(name)) {
      name = "_" + name;
    }
    if (name.length > limit) {
      name = name.slice(0, limit).replace(/[.\s]+$/, "");
      if (!name) {
        name = "output";
      }
    }
    if (hls) {
      return name;
    }
    if (!/\.mp4$/i.test(name)) {
      name += ".mp4";
    }
    return name;
  }

  function deriveOutputName(rawUrl, format) {
    let host = "stream";
    let base = "";
    try {
      const url = new URL(rawUrl);
      host = url.hostname || "stream";
      const segment = url.pathname.split("/").filter(Boolean).pop() || "";
      base = decodeURIComponent(segment).replace(/\.m3u8?$/i, "");
    } catch (_) {
      base = "";
    }
    const combined = [host, base].filter(Boolean).join("-") || "output";
    return sanitizeOutputName(combined, format === "hls" ? "hls" : "mp4");
  }

  /** 标题可用时用标题做输出名，否则回退到 URL 派生名。 */
  function outputNameFromTitle(rawTitle, rawUrl, format) {
    const title = cleanPageTitle(rawTitle);
    if (title) {
      return sanitizeOutputName(title, format);
    }
    return deriveOutputName(rawUrl, format);
  }

  /** 53 位字符串哈希（cyrb53），扩展里需要同步、确定性的短 hash。 */
  function cyrb53(text) {
    let h1 = 0xdeadbeef;
    let h2 = 0x41c6ce57;
    for (let i = 0; i < text.length; i += 1) {
      const ch = text.charCodeAt(i);
      h1 = Math.imul(h1 ^ ch, 2654435761);
      h2 = Math.imul(h2 ^ ch, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507);
    h1 ^= Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507);
    h2 ^= Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return 4294967296 * (2097151 & h2) + (h1 >>> 0);
  }

  /** URL 的 12 位十六进制短 hash，用作纯 hash 文件名。 */
  function urlShortHash(rawUrl) {
    return cyrb53(String(rawUrl || ""))
      .toString(16)
      .padStart(13, "0")
      .slice(-12);
  }

  /** 按命名策略生成输出名：title（页面标题，默认）或 hash（URL 短 hash）。 */
  function buildOutputName(opts) {
    const options = opts || {};
    const format = options.format === "hls" ? "hls" : "mp4";
    if (options.naming === "hash") {
      return sanitizeOutputName(urlShortHash(options.url), format);
    }
    return outputNameFromTitle(options.title, options.url, format);
  }

  /**
   * 追加时间戳换名。limit 为结果长度上限；长标题会被服务端按路径长度截短，
   * 因此重试时应传入较小上限，确保时间戳落在保留的前缀里。
   */
  function addTimestampSuffix(name, format, stamp, limit) {
    const hls = format === "hls";
    let base = String(name || "");
    if (!hls && /\.mp4$/i.test(base)) {
      base = base.slice(0, -".mp4".length);
    }
    const now = stamp || new Date().toISOString();
    const suffix = String(now).replace(/[-:TZ.]/g, "").slice(0, 14);
    const cap = Number.isInteger(limit) && limit > 0 ? limit : OUTPUT_LIMIT;
    const room = Math.max(
      1,
      cap - suffix.length - 1 - (hls ? 0 : ".mp4".length),
    );
    base = base.slice(0, room);
    return sanitizeOutputName(base + "-" + suffix, hls ? "hls" : "mp4");
  }

  function shellQuote(value) {
    const text = String(value == null ? "" : value);
    if (text === "") {
      return "''";
    }
    if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(text)) {
      return text;
    }
    return "'" + text.replace(/'/g, "'\\''") + "'";
  }

  function buildCliCommand(opts) {
    const options = opts || {};
    const format = options.format === "hls" ? "hls" : "mp4";
    const parts = ["m3u8-cli", "download", shellQuote(options.url)];
    parts.push("-f", format);
    if (options.output) {
      parts.push("-o", shellQuote(options.output));
    }
    if (options.referer) {
      parts.push("--referer", shellQuote(options.referer));
    }
    if (options.cookie) {
      parts.push("--cookie", shellQuote(options.cookie));
    }
    if (options.userAgent) {
      parts.push("--user-agent", shellQuote(options.userAgent));
    }
    if (options.overwrite) {
      parts.push("--overwrite");
    }
    return parts.join(" ");
  }

  const api = {
    looksLikePlaylistUrl,
    isHlsContentType,
    normalizeCandidateUrl,
    cleanPageTitle,
    sanitizeOutputName,
    deriveOutputName,
    outputNameFromTitle,
    urlShortHash,
    buildOutputName,
    addTimestampSuffix,
    shellQuote,
    buildCliCommand,
  };

  root.M3U8Lib = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);

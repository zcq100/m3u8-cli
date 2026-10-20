/*
 * m3u8-cli 链接检测器：background / content / popup 共用的纯函数。
 * 以经典脚本方式加载（无构建），同时兼容 Node 的 require。
 */
(function (root) {
  "use strict";

  const HLS_CONTENT_TYPE =
    /(?:application|audio)\/(?:vnd\.apple\.|x-)?mpegurl/i;
  const OUTPUT_LIMIT = 200;
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

  function addTimestampSuffix(name, format, stamp) {
    const hls = format === "hls";
    let base = String(name || "");
    if (!hls && /\.mp4$/i.test(base)) {
      base = base.slice(0, -".mp4".length);
    }
    const now = stamp || new Date().toISOString();
    const suffix = String(now).replace(/[-:TZ.]/g, "").slice(0, 14);
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
    sanitizeOutputName,
    deriveOutputName,
    addTimestampSuffix,
    shellQuote,
    buildCliCommand,
  };

  root.M3U8Lib = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);

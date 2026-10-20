/*
 * 页面内扫描：video/audio/source、m3u8 链接和脚本中的 URL。
 * 网络层（background 的 webRequest）是主要来源，本脚本补充尚未发起请求的静态链接。
 */
"use strict";

(function () {
  const { looksLikePlaylistUrl, normalizeCandidateUrl } = M3U8Lib;

  const MAX_URLS = 100;
  const MAX_SCRIPT_TEXT = 2 * 1024 * 1024;
  const SCAN_DELAY_MS = 500;
  const ABSOLUTE_URL =
    /(?:https?:)?\/\/[^\s"'`<>\\]+?\.m3u8(?:\?[^\s"'`<>\\]*)?/gi;
  const QUOTED_URL = /["'`]([^"'`\s<>]+?\.m3u8(?:\?[^"'`\s<>]*)?)["'`]/gi;

  const seen = new Set();
  let scannedScripts = new WeakSet();
  let scanTimer = null;

  function collectUrl(raw, found, base) {
    if (!raw || seen.size >= MAX_URLS) {
      return;
    }
    const url = normalizeCandidateUrl(raw, base || location.href);
    if (!url || !looksLikePlaylistUrl(url) || seen.has(url)) {
      return;
    }
    seen.add(url);
    found.push(url);
  }

  function extractFromText(text, found) {
    const source = String(text || "").replace(/\\\//g, "/");
    let match;
    ABSOLUTE_URL.lastIndex = 0;
    while ((match = ABSOLUTE_URL.exec(source)) !== null) {
      collectUrl(match[0], found, location.href);
      if (found.length >= MAX_URLS) {
        return;
      }
    }
    QUOTED_URL.lastIndex = 0;
    while ((match = QUOTED_URL.exec(source)) !== null) {
      collectUrl(match[1], found, location.href);
      if (found.length >= MAX_URLS) {
        return;
      }
    }
  }

  function collectFromScripts(found) {
    let budget = MAX_SCRIPT_TEXT;
    let scripts;
    try {
      scripts = document.querySelectorAll("script:not([src])");
    } catch (_) {
      return;
    }
    for (const script of scripts) {
      if (scannedScripts.has(script)) {
        continue;
      }
      scannedScripts.add(script);
      const text = script.textContent || "";
      if (!text) {
        continue;
      }
      budget -= text.length;
      if (budget < 0) {
        break;
      }
      extractFromText(text, found);
      if (found.length >= MAX_URLS) {
        return;
      }
    }
  }

  function scanNow() {
    const found = [];

    for (const media of document.querySelectorAll("video, audio")) {
      collectUrl(media.currentSrc, found);
      collectUrl(media.getAttribute("src"), found);
      if (found.length >= MAX_URLS) {
        break;
      }
    }

    if (found.length < MAX_URLS) {
      for (const source of document.querySelectorAll("source[src]")) {
        const value = source.getAttribute("src");
        if (value && /\.m3u8?(\?|#|$)/i.test(value)) {
          collectUrl(value, found);
        }
        if (found.length >= MAX_URLS) {
          break;
        }
      }
    }

    if (found.length < MAX_URLS) {
      for (const anchor of document.querySelectorAll("a[href]")) {
        const value = anchor.getAttribute("href");
        if (value && /\.m3u8?(\?|#|$)/i.test(value)) {
          collectUrl(value, found);
        }
        if (found.length >= MAX_URLS) {
          break;
        }
      }
    }

    if (found.length < MAX_URLS) {
      collectFromScripts(found);
    }

    if (found.length) {
      report(found);
    }
  }

  function report(urls) {
    try {
      const pending = chrome.runtime.sendMessage({
        type: "dom:candidates",
        urls,
        pageUrl: location.href,
      });
      if (pending && typeof pending.catch === "function") {
        pending.catch(() => {});
      }
    } catch (_) {
      // 扩展重载后旧页面里的 content script 会失效，忽略即可。
    }
  }

  function scheduleScan(delay) {
    if (scanTimer !== null) {
      return;
    }
    scanTimer = window.setTimeout(() => {
      scanTimer = null;
      scanNow();
    }, typeof delay === "number" ? delay : SCAN_DELAY_MS);
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (!message || typeof message.type !== "string") {
      return;
    }
    if (message.type === "dom:rescan") {
      scheduleScan(0);
    } else if (message.type === "dom:reset") {
      seen.clear();
      scannedScripts = new WeakSet();
    }
  });

  const observer = new MutationObserver(() => scheduleScan(SCAN_DELAY_MS));
  try {
    observer.observe(document.documentElement || document, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["src", "href"],
    });
  } catch (_) {
    // 极端情况下 documentElement 不可用，仍保留首屏扫描。
  }

  window.addEventListener("popstate", () => scheduleScan(200));
  window.addEventListener("hashchange", () => scheduleScan(200));
  window.addEventListener("load", () => scheduleScan(100), { once: true });

  scheduleScan(100);
})();

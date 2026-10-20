"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const lib = require("./lib.js");

test("looksLikePlaylistUrl 识别常见 m3u8 URL", () => {
  assert.equal(lib.looksLikePlaylistUrl("https://a.test/hls/index.m3u8"), true);
  assert.equal(
    lib.looksLikePlaylistUrl("https://a.test/hls/index.m3u8?token=abc&x=1"),
    true,
  );
  assert.equal(lib.looksLikePlaylistUrl("https://a.test/list.m3u"), true);
  assert.equal(lib.looksLikePlaylistUrl("HTTPS://A.TEST/INDEX.M3U8"), true);
  assert.equal(lib.looksLikePlaylistUrl("https://a.test/segment.ts"), false);
  assert.equal(lib.looksLikePlaylistUrl("blob:https://a.test/123"), false);
  assert.equal(lib.looksLikePlaylistUrl("/relative/index.m3u8"), false);
  assert.equal(lib.looksLikePlaylistUrl("not a url"), false);
});

test("isHlsContentType 匹配 HLS MIME 类型", () => {
  assert.equal(
    lib.isHlsContentType("application/vnd.apple.mpegurl"),
    true,
  );
  assert.equal(
    lib.isHlsContentType("application/x-mpegURL; charset=utf-8"),
    true,
  );
  assert.equal(lib.isHlsContentType("audio/mpegurl"), true);
  assert.equal(lib.isHlsContentType("video/mp2t"), false);
  assert.equal(lib.isHlsContentType("application/octet-stream"), false);
  assert.equal(lib.isHlsContentType(""), false);
});

test("normalizeCandidateUrl 解析相对路径并拒绝非 HTTP 协议", () => {
  assert.equal(
    lib.normalizeCandidateUrl("index.m3u8", "https://a.test/hls/"),
    "https://a.test/hls/index.m3u8",
  );
  assert.equal(
    lib.normalizeCandidateUrl("/live.m3u8?x=1", "https://a.test/hls/"),
    "https://a.test/live.m3u8?x=1",
  );
  assert.equal(lib.normalizeCandidateUrl("data:text/plain,x", "https://a.test/"), null);
  assert.equal(lib.normalizeCandidateUrl("", "https://a.test/"), null);
  assert.equal(lib.normalizeCandidateUrl(42, "https://a.test/"), null);
});

test("sanitizeOutputName 清洗非法字符并补扩展名", () => {
  assert.equal(lib.sanitizeOutputName("a/b\\c:d*e?f", "mp4"), "a_b_c_d_e_f.mp4");
  assert.equal(lib.sanitizeOutputName("  ..name..  ", "mp4"), "name.mp4");
  assert.equal(lib.sanitizeOutputName("movie.m3u8", "mp4"), "movie.mp4");
  assert.equal(lib.sanitizeOutputName("movie.m3u8", "hls"), "movie");
  assert.equal(lib.sanitizeOutputName("CON", "mp4"), "_CON.mp4");
  assert.equal(lib.sanitizeOutputName("", "mp4"), "output.mp4");
  assert.equal(lib.sanitizeOutputName("already.mp4", "mp4"), "already.mp4");
});

test("sanitizeOutputName 保证不超过 200 字符", () => {
  const long = "x".repeat(500);
  const mp4 = lib.sanitizeOutputName(long, "mp4");
  const hls = lib.sanitizeOutputName(long, "hls");
  assert.equal(mp4.length, 200);
  assert.ok(mp4.endsWith(".mp4"));
  assert.equal(hls.length, 200);
});

test("deriveOutputName 使用 hostname 避免 index 重名", () => {
  assert.equal(
    lib.deriveOutputName("https://cdn.example.com/live/index.m3u8", "mp4"),
    "cdn.example.com-index.mp4",
  );
  assert.equal(
    lib.deriveOutputName("https://cdn.example.com/", "hls"),
    "cdn.example.com",
  );
});

test("addTimestampSuffix 生成确定性的新文件名", () => {
  assert.equal(
    lib.addTimestampSuffix("movie.mp4", "mp4", "2026-10-20T14:30:00.000Z"),
    "movie-20261020143000.mp4",
  );
  assert.equal(
    lib.addTimestampSuffix("movie", "hls", "2026-10-20T14:30:00.000Z"),
    "movie-20261020143000",
  );
});

test("shellQuote 对特殊字符做 POSIX 转义", () => {
  assert.equal(lib.shellQuote("simple-1.m3u8"), "simple-1.m3u8");
  assert.equal(lib.shellQuote("a b"), "'a b'");
  assert.equal(lib.shellQuote("it's"), "'it'\\''s'");
  assert.equal(lib.shellQuote(""), "''");
  assert.equal(lib.shellQuote("a&b"), "'a&b'");
});

test("buildCliCommand 组装下载命令与可选请求头", () => {
  const basic = lib.buildCliCommand({
    url: "https://a.test/index.m3u8",
    format: "mp4",
    output: "a.test-index.mp4",
  });
  assert.equal(
    basic,
    "m3u8-cli download https://a.test/index.m3u8 -f mp4 -o a.test-index.mp4",
  );

  const withHeaders = lib.buildCliCommand({
    url: "https://a.test/index.m3u8?x=1",
    format: "hls",
    output: "movie",
    referer: "https://a.test/page",
    cookie: "sid=abc; theme=dark",
    userAgent: "Mozilla/5.0 Test",
    overwrite: true,
  });
  assert.ok(withHeaders.includes("-f hls"));
  assert.ok(withHeaders.includes("--referer https://a.test/page"));
  assert.ok(withHeaders.includes("--cookie 'sid=abc; theme=dark'"));
  assert.ok(withHeaders.includes("--user-agent 'Mozilla/5.0 Test'"));
  assert.ok(withHeaders.endsWith("--overwrite"));
});

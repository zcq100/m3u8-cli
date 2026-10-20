/*
 * service worker：网络层识别 m3u8、维护每个标签页的候选列表、
 * 调用本地 m3u8-cli serve API 并在后台轮询任务进度。
 */
"use strict";

importScripts("lib.js");

const {
  looksLikePlaylistUrl,
  isHlsContentType,
  normalizeCandidateUrl,
  cleanPageTitle,
  buildOutputName,
  buildCliCommand,
} = M3U8Lib;

const TRACKED_TYPES = [
  "xmlhttprequest",
  "media",
  "other",
  "object",
  "main_frame",
  "sub_frame",
];
const TRACKED_TYPE_SET = new Set(TRACKED_TYPES);
const MAX_CANDIDATES_PER_TAB = 30;
const MAX_PENDING_HEADERS = 128;
const MAX_TASKS = 20;
const MAX_COOKIE_LENGTH = 8192;
const MAX_REFERER_LENGTH = 2048;
const MAX_USER_AGENT_LENGTH = 1024;
const POLL_ALARM = "m3u8-task-poll";
const TERMINAL_STATES = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "lost",
]);
const DEFAULT_SETTINGS = {
  serverUrl: "http://127.0.0.1:8787",
  token: "",
  format: "mp4",
  naming: "title",
  overwrite: false,
  sendCookie: true,
  copyCookie: false,
};

/** requestId -> 白名单请求头快照；service worker 休眠后丢失，可接受。 */
const pendingHeaders = new Map();
let writeChain = Promise.resolve();
let pollInFlight = false;

/* ------------------------------------------------------------------ */
/* 存储工具                                                            */
/* ------------------------------------------------------------------ */

function enqueueWrite(task) {
  const run = writeChain.then(task, task);
  writeChain = run.catch(() => {});
  return run;
}

function tabKey(tabId) {
  return `tab:${tabId}`;
}

function headerKey(candidateId) {
  return `hdr:${candidateId}`;
}

function emptyTabState() {
  return { pageUrl: "", pageTitle: "", updatedAt: 0, items: [] };
}

async function readTabState(tabId) {
  const key = tabKey(tabId);
  const data = await chrome.storage.session.get(key);
  return data[key] || emptyTabState();
}

async function removeHeaders(ids) {
  if (!ids.length) {
    return;
  }
  await chrome.storage.session.remove(ids.map(headerKey));
}

async function updateBadge(tabId, count) {
  try {
    await chrome.action.setBadgeBackgroundColor({
      tabId,
      color: "#7c3aed",
    });
    await chrome.action.setBadgeText({
      tabId,
      text: count > 0 ? String(count) : "",
    });
  } catch (_) {
    // 标签页可能已经关闭。
  }
}

async function clearTab(tabId) {
  if (!Number.isInteger(tabId) || tabId < 0) {
    return;
  }
  return enqueueWrite(async () => {
    const key = tabKey(tabId);
    const data = await chrome.storage.session.get(key);
    const state = data[key];
    if (state && Array.isArray(state.items)) {
      await removeHeaders(state.items.map((item) => item.id));
    }
    await chrome.storage.session.remove(key);
    await updateBadge(tabId, 0);
  });
}

/**
 * 合并候选。同一 URL 保留原 id，网络候选可以升级页面扫描出的候选并补头。
 */
function addCandidate(tabId, candidate, headers) {
  if (!Number.isInteger(tabId) || tabId < 0) {
    return Promise.resolve(null);
  }
  return enqueueWrite(async () => {
    const key = tabKey(tabId);
    const data = await chrome.storage.session.get(key);
    const state = data[key] || emptyTabState();
    const existingIndex = state.items.findIndex(
      (item) => item.url === candidate.url,
    );
    // 空标题不覆盖已有标题：网络候选可能晚于 DOM 候选、拿不到标题。
    const patch = { ...candidate };
    if (!patch.pageTitle) {
      delete patch.pageTitle;
    }

    let item;
    if (existingIndex >= 0) {
      const existing = state.items[existingIndex];
      state.items.splice(existingIndex, 1);
      item = { ...existing, ...patch, id: existing.id };
    } else {
      item = {
        id: crypto.randomUUID(),
        capturedAt: Date.now(),
        ...patch,
      };
    }
    item.hasCookie = Boolean(headers && headers.cookie);

    state.items.unshift(item);
    if (state.items.length > MAX_CANDIDATES_PER_TAB) {
      const removed = state.items.splice(MAX_CANDIDATES_PER_TAB);
      await removeHeaders(removed.map((entry) => entry.id));
    }
    if (!state.pageUrl && candidate.pageUrl) {
      state.pageUrl = candidate.pageUrl;
    }
    if (patch.pageTitle) {
      state.pageTitle = patch.pageTitle;
    }
    state.updatedAt = Date.now();

    const updates = { [key]: state };
    if (headers && (headers.cookie || headers.referer || headers.userAgent)) {
      updates[headerKey(item.id)] = headers;
    }
    await chrome.storage.session.set(updates);
    await updateBadge(tabId, state.items.length);
    return item;
  });
}

/* ------------------------------------------------------------------ */
/* 网络检测                                                            */
/* ------------------------------------------------------------------ */

function headerValue(requestHeaders, name) {
  const wanted = name.toLowerCase();
  for (const header of requestHeaders || []) {
    if (String(header.name).toLowerCase() === wanted) {
      return header.value == null ? "" : String(header.value);
    }
  }
  return "";
}

function pickHeaders(requestHeaders) {
  return {
    cookie: headerValue(requestHeaders, "cookie").slice(0, MAX_COOKIE_LENGTH),
    referer: headerValue(requestHeaders, "referer").slice(0, MAX_REFERER_LENGTH),
    userAgent: headerValue(requestHeaders, "user-agent").slice(
      0,
      MAX_USER_AGENT_LENGTH,
    ),
  };
}

function safeHost(value) {
  try {
    return new URL(value).hostname;
  } catch (_) {
    return "";
  }
}

function trimPendingHeaders() {
  while (pendingHeaders.size > MAX_PENDING_HEADERS) {
    const oldest = pendingHeaders.keys().next().value;
    pendingHeaders.delete(oldest);
  }
}

/** 标签页标题；host permission 已覆盖所有 http/https 页面。 */
async function getTabTitle(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    return tab && typeof tab.title === "string" ? tab.title : "";
  } catch (_) {
    return "";
  }
}

chrome.webRequest.onSendHeaders.addListener(
  (details) => {
    if (details.tabId < 0 || !TRACKED_TYPE_SET.has(details.type)) {
      return;
    }
    const headers = pickHeaders(details.requestHeaders);
    if (!headers.cookie && !headers.referer && !headers.userAgent) {
      return;
    }
    pendingHeaders.set(details.requestId, {
      ...headers,
      tabId: details.tabId,
      frameId: details.frameId,
    });
    trimPendingHeaders();
  },
  { urls: ["<all_urls>"], types: TRACKED_TYPES },
  ["requestHeaders", "extraHeaders"],
);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0 || !TRACKED_TYPE_SET.has(details.type)) {
      return;
    }
    if (details.method !== "GET" && details.method !== "HEAD") {
      return;
    }
    if (details.statusCode < 200 || details.statusCode >= 300) {
      return;
    }
    const contentType = headerValue(details.responseHeaders, "content-type");
    if (!looksLikePlaylistUrl(details.url) && !isHlsContentType(contentType)) {
      return;
    }

    const captured = pendingHeaders.get(details.requestId) || {};
    const pageUrl =
      [details.documentUrl, details.initiator].find(
        (value) => typeof value === "string" && /^https?:/i.test(value),
      ) || "";
    const referer = captured.referer || pageUrl;
    const candidate = {
      url: details.url,
      source: "network",
      contentType: contentType.split(";")[0].trim(),
      refererHost: referer ? safeHost(referer) : "",
      pageUrl,
    };
    const headers = {
      cookie: captured.cookie || "",
      referer,
      userAgent: captured.userAgent || "",
    };
    getTabTitle(details.tabId)
      .then((rawTitle) => {
        const pageTitle = cleanPageTitle(rawTitle);
        return addCandidate(
          details.tabId,
          pageTitle ? { ...candidate, pageTitle } : candidate,
          headers,
        );
      })
      .catch(() => {});
  },
  { urls: ["<all_urls>"], types: TRACKED_TYPES },
  ["responseHeaders"],
);

function forgetRequest(details) {
  pendingHeaders.delete(details.requestId);
}

chrome.webRequest.onCompleted.addListener(forgetRequest, {
  urls: ["<all_urls>"],
  types: TRACKED_TYPES,
});

chrome.webRequest.onErrorOccurred.addListener(forgetRequest, {
  urls: ["<all_urls>"],
  types: TRACKED_TYPES,
});

/* ------------------------------------------------------------------ */
/* 标签页生命周期                                                      */
/* ------------------------------------------------------------------ */

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "loading" || changeInfo.url) {
    clearTab(tabId).catch(() => {});
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  clearTab(tabId).catch(() => {});
});

async function handleDomCandidates(message, sender) {
  const tabId = sender.tab && sender.tab.id;
  if (!Number.isInteger(tabId) || tabId < 0) {
    return;
  }
  const pageUrl = (sender.tab && sender.tab.url) || message.pageUrl || "";
  const base = message.pageUrl || pageUrl;
  const urls = Array.isArray(message.urls) ? message.urls.slice(0, 100) : [];
  const seen = new Set();
  const tabTitle = cleanPageTitle((sender.tab && sender.tab.title) || "");
  // iframe 里的 document.title 往往只是播放器自己的标题，优先用顶层标签页标题。
  const pageTitle =
    message.frameTop === true
      ? cleanPageTitle(message.title) || tabTitle
      : tabTitle || cleanPageTitle(message.title);

  for (const raw of urls) {
    const url = normalizeCandidateUrl(raw, base);
    if (!url || seen.has(url) || !looksLikePlaylistUrl(url)) {
      continue;
    }
    seen.add(url);
    const candidate = {
      url,
      source: "dom",
      contentType: "",
      refererHost: "",
      pageUrl,
    };
    if (pageTitle) {
      candidate.pageTitle = pageTitle;
    }
    await addCandidate(tabId, candidate, null);
  }
}

async function clearTabAndRescan(tabId) {
  await clearTab(tabId);
  try {
    await chrome.tabs.sendMessage(tabId, { type: "dom:reset" });
  } catch (_) {
    // 页面可能没有 content script（例如 chrome:// 页面）。
  }
  return { ok: true };
}

async function rescanTab(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "dom:rescan" });
  } catch (_) {
    // 扩展重载后旧页面没有 content script，需要用户刷新。
  }
  return { ok: true };
}

/* ------------------------------------------------------------------ */
/* 设置与 serve API                                                    */
/* ------------------------------------------------------------------ */

async function getSettings() {
  const data = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(data.settings || {}) };
}

function normalizeServerUrl(value) {
  const text = String(value || "").trim().replace(/\/+$/, "");
  if (!text) {
    return DEFAULT_SETTINGS.serverUrl;
  }
  return /^https?:\/\//i.test(text) ? text : `http://${text}`;
}

function resolveFormat(requested, fallback) {
  if (requested === "hls" || requested === "mp4") {
    return requested;
  }
  return fallback === "hls" ? "hls" : "mp4";
}

function resolveNaming(requested, fallback) {
  if (requested === "hash" || requested === "title") {
    return requested;
  }
  return fallback === "hash" ? "hash" : "title";
}

async function apiFetch(path, options) {
  const settings = await getSettings();
  const base = normalizeServerUrl(settings.serverUrl);
  const init = {
    ...(options || {}),
    headers: { ...((options && options.headers) || {}) },
    signal: AbortSignal.timeout(8000),
  };
  init.headers.Authorization = `Bearer ${settings.token}`;

  let response;
  try {
    response = await fetch(base + path, init);
  } catch (error) {
    const name = error && error.name;
    if (name === "TimeoutError" || name === "AbortError") {
      throw new Error(
        `连接 ${base} 超时（8 秒）：请确认服务已启动且地址可达`,
      );
    }
    const reason = (error && error.message) || String(error);
    throw new Error(
      `无法连接 ${base}（${reason}）：请确认 m3u8-cli serve 已启动、地址和端口正确`,
    );
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  if (!response.ok) {
    const error = new Error(
      (payload && payload.error) || `HTTP ${response.status}`,
    );
    error.status = response.status;
    throw error;
  }
  return payload;
}

/** 按命名策略生成输出名；标题缺失时回退到 URL 派生名。 */
function candidateOutputName(candidate, state, format, naming) {
  const title = candidate.pageTitle || (state && state.pageTitle) || "";
  return buildOutputName({
    title,
    url: candidate.url,
    format,
    naming,
  });
}

async function submitDownload(message) {
  const tabId = message.tabId;
  if (!Number.isInteger(tabId) || tabId < 0) {
    return { ok: false, error: "无效的标签页" };
  }
  const settings = await getSettings();
  if (!settings.token) {
    return {
      ok: false,
      error: "未配置 Token：请在扩展设置中填写 serve 的 token",
    };
  }
  const state = await readTabState(tabId);
  const candidate = state.items.find((item) => item.id === message.candidateId);
  if (!candidate) {
    return { ok: false, error: "未找到该链接，请重新扫描页面" };
  }

  const key = headerKey(candidate.id);
  const captured = (await chrome.storage.session.get(key))[key] || {};
  const format = resolveFormat(message.format, settings.format);
  const naming = resolveNaming(message.naming, settings.naming);
  const output =
    message.output ||
    candidateOutputName(candidate, state, format, naming);
  const overwrite =
    message.overwrite === undefined
      ? Boolean(settings.overwrite)
      : Boolean(message.overwrite);
  const referer = captured.referer || state.pageUrl || "";
  const userAgent = captured.userAgent || navigator.userAgent;
  const headers = {};
  if (referer) {
    headers.Referer = referer;
  }
  if (userAgent) {
    headers["User-Agent"] = userAgent;
  }
  if (settings.sendCookie && captured.cookie) {
    headers.Cookie = captured.cookie;
  }

  const result = await apiFetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url: candidate.url,
      format,
      output,
      overwrite,
      headers,
    }),
  });
  const task = summarizeTask(result.task);
  await upsertTask(task);
  await ensurePollAlarm();
  return { ok: true, task };
}

async function testConnection() {
  const settings = await getSettings();
  if (!settings.token) {
    return { ok: false, error: "请先填写 Token" };
  }
  try {
    const result = await apiFetch("/api/tasks?limit=1");
    return { ok: true, total: Number(result.total || 0) };
  } catch (error) {
    return { ok: false, error: error.message, status: error.status || 0 };
  }
}

async function buildCliMessage(message) {
  const tabId = message.tabId;
  if (!Number.isInteger(tabId) || tabId < 0) {
    return { ok: false, error: "无效的标签页" };
  }
  const state = await readTabState(tabId);
  const candidate = state.items.find((item) => item.id === message.candidateId);
  if (!candidate) {
    return { ok: false, error: "未找到该链接，请重新扫描页面" };
  }
  const settings = await getSettings();
  const key = headerKey(candidate.id);
  const captured = (await chrome.storage.session.get(key))[key] || {};
  const format = resolveFormat(message.format, settings.format);
  const naming = resolveNaming(message.naming, settings.naming);
  const output =
    message.output ||
    candidateOutputName(candidate, state, format, naming);
  const includeCookie = Boolean(message.includeCookie);
  const overwrite =
    message.overwrite === undefined
      ? Boolean(settings.overwrite)
      : Boolean(message.overwrite);
  const command = buildCliCommand({
    url: candidate.url,
    format,
    output,
    referer: captured.referer || state.pageUrl || "",
    cookie: includeCookie ? captured.cookie || "" : "",
    userAgent: captured.userAgent || "",
    overwrite,
  });
  return { ok: true, command };
}

/* ------------------------------------------------------------------ */
/* 任务记录与轮询                                                      */
/* ------------------------------------------------------------------ */

function summarizeTask(task) {
  return {
    id: task.id,
    url: task.url,
    format: task.format,
    output: task.output,
    state: task.state,
    completed: Number(task.completed || 0),
    total: Number(task.total || 0),
    error: task.error || "",
    updatedAt: Date.now(),
  };
}

async function getTasks() {
  const data = await chrome.storage.local.get("tasks");
  return Array.isArray(data.tasks) ? data.tasks : [];
}

function trimTasks(tasks) {
  const active = tasks.filter((task) => !TERMINAL_STATES.has(task.state));
  const terminal = tasks.filter((task) => TERMINAL_STATES.has(task.state));
  return [...active, ...terminal.slice(0, Math.max(0, MAX_TASKS - active.length))];
}

async function upsertTask(task) {
  const tasks = await getTasks();
  const index = tasks.findIndex((entry) => entry.id === task.id);
  if (index >= 0) {
    tasks[index] = { ...tasks[index], ...task };
  } else {
    tasks.unshift({ ...task, createdAt: Date.now() });
  }
  await chrome.storage.local.set({ tasks: trimTasks(tasks) });
}

async function cancelTask(taskId) {
  if (!taskId) {
    return { ok: false, error: "缺少任务 ID" };
  }
  try {
    const result = await apiFetch(
      `/api/tasks/${encodeURIComponent(taskId)}`,
      { method: "DELETE" },
    );
    const task = summarizeTask(result.task);
    await upsertTask(task);
    await ensurePollAlarm();
    return { ok: true, task };
  } catch (error) {
    return { ok: false, error: error.message, status: error.status || 0 };
  }
}

async function pollTasks() {
  if (pollInFlight) {
    return getTasks();
  }
  pollInFlight = true;
  try {
    const tasks = await getTasks();
    let authFailed = false;

    for (const task of tasks) {
      if (TERMINAL_STATES.has(task.state) || authFailed) {
        continue;
      }
      try {
        const result = await apiFetch(
          `/api/tasks/${encodeURIComponent(task.id)}`,
        );
        Object.assign(task, summarizeTask(result.task));
      } catch (error) {
        if (error.status === 401) {
          authFailed = true;
          task.error = "Token 无效或已变化";
        } else if (error.status === 404) {
          task.state = "lost";
          task.error = "服务端已清理该任务";
        } else {
          task.error = error.message;
        }
        task.updatedAt = Date.now();
      }
    }

    await chrome.storage.local.set({ tasks: trimTasks(tasks) });
    const allFinished = tasks.every((task) =>
      TERMINAL_STATES.has(task.state),
    );
    if (allFinished || authFailed) {
      await chrome.alarms.clear(POLL_ALARM);
    }
    return tasks;
  } finally {
    pollInFlight = false;
  }
}

async function ensurePollAlarm() {
  const tasks = await getTasks();
  if (tasks.some((task) => !TERMINAL_STATES.has(task.state))) {
    await chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
  }
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) {
    pollTasks().catch(() => {});
  }
});

/* ------------------------------------------------------------------ */
/* 生命周期与消息路由                                                  */
/* ------------------------------------------------------------------ */

chrome.runtime.onInstalled.addListener(async () => {
  const data = await chrome.storage.local.get("settings");
  if (!data.settings) {
    await chrome.storage.local.set({ settings: DEFAULT_SETTINGS });
  }
  await ensurePollAlarm();
});

chrome.runtime.onStartup.addListener(() => {
  ensurePollAlarm().catch(() => {});
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const type = message && message.type;
  if (typeof type !== "string") {
    return false;
  }

  if (type === "dom:candidates") {
    handleDomCandidates(message, sender).catch(() => {});
    return false;
  }

  const handlers = {
    "download:submit": () => submitDownload(message),
    "task:cancel": () => cancelTask(message.taskId),
    "tasks:refresh": async () => {
      const tasks = await pollTasks();
      await ensurePollAlarm();
      return { ok: true, tasks };
    },
    "settings:test": () => testConnection(),
    "cli:build": () => buildCliMessage(message),
    "tab:clear": () => clearTabAndRescan(message.tabId),
    "dom:rescan": () => rescanTab(message.tabId),
  };
  const handler = handlers[type];
  if (!handler) {
    return false;
  }
  handler().then(sendResponse, (error) => {
    sendResponse({
      ok: false,
      error: String((error && error.message) || error),
      status: Number((error && error.status) || 0),
    });
  });
  return true;
});

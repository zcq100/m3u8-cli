"use strict";

(function () {
  const { buildOutputName, addTimestampSuffix } = M3U8Lib;

  const DEFAULT_SETTINGS = {
    serverUrl: "http://127.0.0.1:8787",
    token: "",
    format: "mp4",
    naming: "title",
    overwrite: false,
    sendCookie: true,
    copyCookie: false,
  };
  const STATE_LABELS = {
    queued: "排队中",
    running: "下载中",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
    lost: "已丢失",
  };
  const ACTIVE_STATES = new Set(["queued", "running"]);
  const REFRESH_INTERVAL_MS = 2000;
  const SETTINGS_SAVE_DELAY_MS = 300;
  // 服务端会按输出目录长度截短文件名；重试名保持较短，避免时间戳被截掉。
  const RETRY_NAME_LIMIT = 100;

  const elements = {};
  let currentTabId = null;
  let currentPageUrl = "";
  let currentPageTitle = "";
  let candidates = [];
  let tasks = [];
  let settings = { ...DEFAULT_SETTINGS };
  let taskTimer = null;
  let settingsSaveTimer = null;
  let toastTimer = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function cacheElements() {
    elements.candidateCount = byId("candidate-count");
    elements.candidateList = byId("candidate-list");
    elements.emptyState = byId("empty-state");
    elements.rescanButton = byId("rescan-button");
    elements.clearButton = byId("clear-button");
    elements.refreshTasks = byId("refresh-tasks");
    elements.taskList = byId("task-list");
    elements.taskEmpty = byId("task-empty");
    elements.serverUrl = byId("server-url");
    elements.token = byId("token");
    elements.format = byId("format");
    elements.naming = byId("naming");
    elements.overwrite = byId("overwrite");
    elements.sendCookie = byId("send-cookie");
    elements.copyCookie = byId("copy-cookie");
    elements.remoteWarning = byId("remote-warning");
    elements.testConnection = byId("test-connection");
    elements.settingsStatus = byId("settings-status");
    elements.toast = byId("toast");
  }

  /* ------------------------------------------------------------------ */
  /* 通信与基础工具                                                      */
  /* ------------------------------------------------------------------ */

  function sendMessage(message) {
    return new Promise((resolve, reject) => {
      try {
        chrome.runtime.sendMessage(message, (response) => {
          const error = chrome.runtime.lastError;
          if (error) {
            reject(new Error(error.message));
            return;
          }
          resolve(response);
        });
      } catch (error) {
        reject(error);
      }
    });
  }

  function createButton(label, onClick, className) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className || "ghost";
    button.textContent = label;
    button.addEventListener("click", () => {
      Promise.resolve(onClick(button)).catch((error) => {
        showToast(error.message || String(error));
      });
    });
    return button;
  }

  function showToast(text, duration) {
    elements.toast.textContent = text;
    elements.toast.hidden = false;
    if (toastTimer) {
      clearTimeout(toastTimer);
    }
    toastTimer = window.setTimeout(() => {
      elements.toast.hidden = true;
      toastTimer = null;
    }, duration || 2600);
  }

  async function copyText(text, label) {
    try {
      await navigator.clipboard.writeText(text);
      showToast(label ? `已复制${label}` : "已复制");
    } catch (error) {
      showToast(`复制失败：${error.message}`);
    }
  }

  /* ------------------------------------------------------------------ */
  /* 候选列表                                                            */
  /* ------------------------------------------------------------------ */

  async function loadCandidates() {
    if (currentTabId === null) {
      candidates = [];
      renderCandidates();
      return;
    }
    const key = `tab:${currentTabId}`;
    const data = await chrome.storage.session.get(key);
    const state = data[key];
    candidates = state && Array.isArray(state.items) ? state.items : [];
    if (state && state.pageUrl) {
      currentPageUrl = state.pageUrl;
    }
    if (state && state.pageTitle) {
      currentPageTitle = state.pageTitle;
    }
    renderCandidates();
  }

  function renderCandidates() {
    elements.candidateCount.textContent = String(candidates.length);
    elements.emptyState.hidden = candidates.length > 0;
    elements.candidateList.replaceChildren();
    for (const candidate of candidates) {
      elements.candidateList.append(buildCandidateItem(candidate));
    }
  }

  function buildCandidateItem(candidate) {
    const item = document.createElement("li");
    item.className = "candidate-item";

    const meta = document.createElement("div");
    meta.className = "candidate-meta";
    const sourceBadge = document.createElement("span");
    sourceBadge.className = "badge";
    sourceBadge.textContent = candidate.source === "network" ? "网络" : "页面";
    meta.append(sourceBadge);
    if (candidate.hasCookie) {
      const cookieBadge = document.createElement("span");
      cookieBadge.className = "badge cookie";
      cookieBadge.textContent = "含 Cookie";
      meta.append(cookieBadge);
    }

    let titleEl = null;
    if (candidate.pageTitle) {
      titleEl = document.createElement("div");
      titleEl.className = "candidate-title";
      titleEl.textContent = candidate.pageTitle;
      titleEl.title = candidate.pageTitle;
    }

    const url = document.createElement("div");
    url.className = "candidate-url";
    url.textContent = candidate.url;
    url.title = candidate.url;

    const actions = document.createElement("div");
    actions.className = "candidate-actions";
    const downloadButton = createButton(
      "下载",
      (button) => handleDownload(candidate, item, button),
      "primary",
    );
    actions.append(
      createButton("复制", () => copyText(candidate.url, "链接")),
      createButton("复制 CLI", () => copyCli(candidate)),
      downloadButton,
    );

    item.append(meta);
    if (titleEl) {
      item.append(titleEl);
    }
    item.append(url, actions);
    return item;
  }

  async function copyCli(candidate) {
    const response = await sendMessage({
      type: "cli:build",
      tabId: currentTabId,
      candidateId: candidate.id,
      format: settings.format,
      naming: settings.naming,
      includeCookie: settings.copyCookie,
      overwrite: settings.overwrite,
    });
    if (!response || !response.ok) {
      showToast((response && response.error) || "生成命令失败");
      return;
    }
    await copyText(response.command, " CLI 命令");
  }

  async function handleDownload(candidate, container, button, outputOverride) {
    button.disabled = true;
    const output =
      outputOverride ||
      buildOutputName({
        title: candidate.pageTitle || currentPageTitle,
        url: candidate.url,
        format: settings.format,
        naming: settings.naming,
      });
    try {
      await saveSettingsNow();
      const response = await sendMessage({
        type: "download:submit",
        tabId: currentTabId,
        candidateId: candidate.id,
        format: settings.format,
        output,
        overwrite: settings.overwrite,
      });
      if (!response || !response.ok) {
        const message = (response && response.error) || "提交失败";
        showToast(message, 6000);
        if (response && response.status === 409) {
          showConflict(container, candidate, output, message);
        } else {
          showSubmitError(container, message);
        }
        return;
      }
      const existingError = container.querySelector(".candidate-error");
      if (existingError) {
        existingError.remove();
      }
      showToast(`已提交：${response.task.output}`);
      await loadTasks();
      startTaskRefresh();
      await refreshTasks();
    } catch (error) {
      showToast(error.message || "提交失败");
    } finally {
      button.disabled = false;
    }
  }

  /** 下载失败时在候选下方保留错误原因，并提供打开设置的入口。 */
  function showSubmitError(container, message) {
    const existing = container.querySelector(".candidate-error");
    if (existing) {
      existing.remove();
    }
    const box = document.createElement("div");
    box.className = "candidate-error";
    const text = document.createElement("span");
    text.textContent = message;
    const settingsButton = createButton(
      "打开设置",
      () => {
        const panel = document.querySelector(".settings");
        if (panel) {
          panel.open = true;
        }
        elements.token.focus();
      },
      "ghost small",
    );
    box.append(text, settingsButton);
    container.append(box);
  }

  function showConflict(container, candidate, output, message) {
    const existing = container.querySelector(".candidate-error");
    if (existing) {
      existing.remove();
    }
    const box = document.createElement("div");
    box.className = "candidate-error";
    const text = document.createElement("span");
    text.textContent = message;
    const retry = createButton(
      "换名重试",
      async (button) => {
        box.remove();
        const newOutput = addTimestampSuffix(
          output,
          settings.format,
          undefined,
          RETRY_NAME_LIMIT,
        );
        await handleDownload(candidate, container, button, newOutput);
      },
      "ghost small",
    );
    box.append(text, retry);
    container.append(box);
  }

  async function requestRescan() {
    try {
      await sendMessage({ type: "dom:rescan", tabId: currentTabId });
      showToast("已请求重新扫描");
      window.setTimeout(() => {
        loadCandidates().catch(() => {});
      }, 700);
    } catch (error) {
      showToast(`重新扫描失败：${error.message}`);
    }
  }

  async function clearCurrentTab() {
    try {
      await sendMessage({ type: "tab:clear", tabId: currentTabId });
      candidates = [];
      currentPageTitle = "";
      renderCandidates();
      showToast("已清空");
    } catch (error) {
      showToast(`清空失败：${error.message}`);
    }
  }

  /* ------------------------------------------------------------------ */
  /* 任务列表                                                            */
  /* ------------------------------------------------------------------ */

  async function loadTasks() {
    const data = await chrome.storage.local.get("tasks");
    tasks = Array.isArray(data.tasks) ? data.tasks : [];
    renderTasks();
  }

  function hasActiveTasks() {
    return tasks.some((task) => ACTIVE_STATES.has(task.state));
  }

  function startTaskRefresh() {
    if (taskTimer !== null) {
      return;
    }
    taskTimer = window.setInterval(() => {
      if (hasActiveTasks()) {
        refreshTasks();
      }
    }, REFRESH_INTERVAL_MS);
  }

  async function refreshTasks() {
    try {
      const response = await sendMessage({ type: "tasks:refresh" });
      if (response && response.ok && Array.isArray(response.tasks)) {
        tasks = response.tasks;
        renderTasks();
      }
    } catch (error) {
      showToast(`刷新任务失败：${error.message}`);
    }
  }

  function renderTasks() {
    elements.taskEmpty.hidden = tasks.length > 0;
    elements.taskList.replaceChildren();
    for (const task of tasks) {
      elements.taskList.append(buildTaskItem(task));
    }
    if (hasActiveTasks()) {
      startTaskRefresh();
    }
  }

  function buildTaskItem(task) {
    const item = document.createElement("li");
    item.className = "task-item";

    const head = document.createElement("div");
    head.className = "task-head";
    const output = document.createElement("span");
    output.className = "task-output";
    output.textContent = task.output || "(未命名)";
    output.title = task.output || "";
    const state = document.createElement("span");
    state.className = `state ${task.state || ""}`;
    state.textContent = STATE_LABELS[task.state] || task.state || "";
    head.append(output, state);

    const url = document.createElement("div");
    url.className = "task-url";
    url.textContent = task.url || "";
    url.title = task.url || "";

    item.append(head, url);

    const total = Number(task.total || 0);
    const completed = Number(task.completed || 0);
    if (ACTIVE_STATES.has(task.state)) {
      const progress = document.createElement("div");
      progress.className = "progress";
      const bar = document.createElement("span");
      if (total > 0) {
        bar.style.width = `${Math.min(100, Math.round((completed / total) * 100))}%`;
      } else {
        progress.classList.add("indeterminate");
      }
      progress.append(bar);
      item.append(progress);
    }

    if (task.error) {
      const error = document.createElement("div");
      error.className = "task-error";
      error.textContent = task.error;
      item.append(error);
    }

    const footer = document.createElement("div");
    footer.className = "task-footer";
    const progressText = document.createElement("span");
    progressText.textContent =
      total > 0 ? `${completed} / ${total} 分片` : "";
    footer.append(progressText);
    if (ACTIVE_STATES.has(task.state)) {
      footer.append(
        createButton(
          "取消",
          async (button) => {
            button.disabled = true;
            const response = await sendMessage({
              type: "task:cancel",
              taskId: task.id,
            });
            if (!response || !response.ok) {
              showToast((response && response.error) || "取消失败");
            }
            await refreshTasks();
          },
          "ghost small",
        ),
      );
    }

    item.append(footer);
    return item;
  }

  /* ------------------------------------------------------------------ */
  /* 设置                                                                */
  /* ------------------------------------------------------------------ */

  async function loadSettings() {
    const data = await chrome.storage.local.get("settings");
    settings = { ...DEFAULT_SETTINGS, ...(data.settings || {}) };
    elements.serverUrl.value = settings.serverUrl;
    elements.token.value = settings.token;
    elements.format.value = settings.format === "hls" ? "hls" : "mp4";
    elements.naming.value = settings.naming === "hash" ? "hash" : "title";
    elements.overwrite.checked = Boolean(settings.overwrite);
    elements.sendCookie.checked = settings.sendCookie !== false;
    elements.copyCookie.checked = Boolean(settings.copyCookie);
    updateRemoteWarning();
  }

  function readSettingsFromForm() {
    return {
      serverUrl:
        elements.serverUrl.value.trim() || DEFAULT_SETTINGS.serverUrl,
      token: elements.token.value.trim(),
      format: elements.format.value === "hls" ? "hls" : "mp4",
      naming: elements.naming.value === "hash" ? "hash" : "title",
      overwrite: elements.overwrite.checked,
      sendCookie: elements.sendCookie.checked,
      copyCookie: elements.copyCookie.checked,
    };
  }

  async function saveSettingsNow() {
    if (settingsSaveTimer) {
      clearTimeout(settingsSaveTimer);
      settingsSaveTimer = null;
    }
    settings = readSettingsFromForm();
    updateRemoteWarning();
    await chrome.storage.local.set({ settings });
  }

  function queueSaveSettings() {
    settings = readSettingsFromForm();
    updateRemoteWarning();
    if (settingsSaveTimer) {
      clearTimeout(settingsSaveTimer);
    }
    settingsSaveTimer = window.setTimeout(() => {
      settingsSaveTimer = null;
      chrome.storage.local
        .set({ settings: readSettingsFromForm() })
        .then(() => showStatus("已保存", "ok"))
        .catch((error) => showStatus(`保存失败：${error.message}`, "error"));
    }, SETTINGS_SAVE_DELAY_MS);
  }

  function isLoopback(value) {
    const text = String(value || "").trim();
    if (!text) {
      return true;
    }
    try {
      const url = new URL(/^[a-z]+:\/\//i.test(text) ? text : `http://${text}`);
      return ["127.0.0.1", "localhost", "::1", "[::1]"].includes(url.hostname);
    } catch (_) {
      return false;
    }
  }

  function updateRemoteWarning() {
    elements.remoteWarning.hidden = !(
      settings.sendCookie && !isLoopback(settings.serverUrl)
    );
  }

  function showStatus(text, kind) {
    elements.settingsStatus.textContent = text;
    elements.settingsStatus.className = `status${kind ? ` ${kind}` : ""}`;
  }

  async function testConnection() {
    elements.testConnection.disabled = true;
    showStatus("连接中…", "");
    try {
      await saveSettingsNow();
      const response = await sendMessage({ type: "settings:test" });
      if (response && response.ok) {
        showStatus(`已连接（共 ${response.total} 个任务）`, "ok");
      } else {
        showStatus((response && response.error) || "连接失败", "error");
      }
    } catch (error) {
      showStatus(`连接失败：${error.message}`, "error");
    } finally {
      elements.testConnection.disabled = false;
    }
  }

  /* ------------------------------------------------------------------ */
  /* 初始化                                                              */
  /* ------------------------------------------------------------------ */

  function bindEvents() {
    elements.rescanButton.addEventListener("click", requestRescan);
    elements.clearButton.addEventListener("click", clearCurrentTab);
    elements.refreshTasks.addEventListener("click", () => {
      refreshTasks().catch(() => {});
    });
    elements.testConnection.addEventListener("click", testConnection);

    for (const input of [
      elements.serverUrl,
      elements.token,
      elements.format,
      elements.naming,
      elements.overwrite,
      elements.sendCookie,
      elements.copyCookie,
    ]) {
      input.addEventListener("input", queueSaveSettings);
      input.addEventListener("change", queueSaveSettings);
    }

    chrome.storage.onChanged.addListener((changes, areaName) => {
      if (areaName === "session" && currentTabId !== null) {
        const key = `tab:${currentTabId}`;
        if (changes[key]) {
          const state = changes[key].newValue || { items: [] };
          candidates = Array.isArray(state.items) ? state.items : [];
          if (state.pageUrl) {
            currentPageUrl = state.pageUrl;
          }
          if (state.pageTitle) {
            currentPageTitle = state.pageTitle;
          }
          renderCandidates();
        }
      } else if (areaName === "local") {
        if (changes.tasks) {
          tasks = Array.isArray(changes.tasks.newValue)
            ? changes.tasks.newValue
            : [];
          renderTasks();
        }
        if (changes.settings) {
          settings = {
            ...DEFAULT_SETTINGS,
            ...(changes.settings.newValue || {}),
          };
          updateRemoteWarning();
        }
      }
    });

    window.addEventListener("unload", () => {
      if (taskTimer !== null) {
        clearInterval(taskTimer);
        taskTimer = null;
      }
    });
  }

  async function init() {
    cacheElements();
    bindEvents();

    const [tab] = await chrome.tabs.query({
      active: true,
      currentWindow: true,
    });
    if (tab) {
      currentTabId = typeof tab.id === "number" ? tab.id : null;
      currentPageUrl = tab.url || "";
      currentPageTitle = tab.title || "";
    }

    await Promise.all([loadSettings(), loadCandidates(), loadTasks()]);
    startTaskRefresh();
    await refreshTasks();
  }

  document.addEventListener("DOMContentLoaded", () => {
    init().catch((error) => {
      showToast(`初始化失败：${error.message}`);
    });
  });
})();

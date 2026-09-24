importScripts("title_utils.js", "bridge_core.js");

const API_BASE = "http://127.0.0.1:23666/api/meeting-bridge";
const BRIDGE_VERSION = "0.1.0";
const HEARTBEAT_INTERVAL_MS = 5000;
const PAGE_TIMEOUT_MS = 45000;
const MEDIA_TIMEOUT_MS = 20000;
const MEDIA_HOSTS = ["tencent.com", "qcloud.com", "myqcloud.com", "tencent-cloud.net", "tencentcs.com"];
const mediaContexts = new Map();
const pendingRequests = new Map();

let bridgeToken = "";
let busy = false;
let activeWorker = null;
let heartbeatTimer = null;
let workerStage = "idle";

function hostAllowed(hostname) {
  const host = String(hostname || "").toLowerCase();
  return MEDIA_HOSTS.some((suffix) => host !== suffix && host.endsWith(`.${suffix}`));
}

function urlAllowed(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && hostAllowed(url.hostname);
  } catch { return false; }
}

function pageInitiator(details) {
  for (const value of [details.initiator, details.documentUrl, details.originUrl]) {
    try {
      if (new URL(value).hostname === "meeting.tencent.com") return true;
    } catch { /* 浏览器可能不给非页面请求提供 initiator。 */ }
  }
  return false;
}

function requestKey(url) {
  try {
    const parsed = new URL(url);
    parsed.hash = "";
    return parsed.href;
  } catch { return String(url); }
}

function ensureToken(force = false) {
  if (bridgeToken && !force) return Promise.resolve(bridgeToken);
  return fetch(`${API_BASE}/pair`, { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) throw new Error("pair rejected");
      const payload = await response.json();
      if (typeof payload.token !== "string" || !payload.token) throw new Error("pair invalid");
      bridgeToken = payload.token;
      return bridgeToken;
    });
}

async function bridgeFetch(path, options = {}, retry = true) {
  const token = await ensureToken();
  const headers = new Headers(options.headers || {});
  headers.set("X-CourseFlow-Bridge-Token", token);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers, cache: "no-store" });
  if (response.status === 403 && retry) {
    bridgeToken = "";
    await ensureToken(true);
    return bridgeFetch(path, options, false);
  }
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`bridge HTTP ${response.status}`);
  return payload;
}

function browserName() {
  return /Edg\//u.test(navigator.userAgent) ? "edge" : "chrome";
}

async function heartbeat() {
  await bridgeFetch("/heartbeat", {
    method: "POST",
    body: JSON.stringify({ version: BRIDGE_VERSION, browser: browserName() })
  });
}

async function readNextTask() {
  return bridgeFetch("/tasks/next");
}

async function waitForTabComplete(tabId, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return tab;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error("PAGE_TIMEOUT");
}

function externalLoginUrl(value) {
  try {
    const url = new URL(value);
    const trustedHost = url.hostname === "meeting.tencent.com" || url.hostname.endsWith(".tencent.com") || url.hostname === "qq.com" || url.hostname.endsWith(".qq.com");
    return trustedHost && /\/(?:login(?:\.html)?|signin|passport|wwlogin|connect\/qrconnect)(?:\/|$)/iu.test(url.pathname);
  } catch { return false; }
}

async function inspectTab(tabId, prepare = false) {
  const tab = await chrome.tabs.get(tabId);
  if (externalLoginUrl(tab.url)) return { url: tab.url, loginRequired: true };
  try {
    return await chrome.tabs.sendMessage(tabId, { type: "inspectRecordingPage", prepare });
  } catch { return null; }
}

async function waitForPage(tabId) {
  const deadline = Date.now() + PAGE_TIMEOUT_MS;
  let firstInspection = true;
  while (Date.now() < deadline) {
    const tab = await waitForTabComplete(tabId, Math.min(3000, Math.max(1, deadline - Date.now()))).catch((error) => {
      if (error?.message === "PAGE_TIMEOUT") return null;
      throw error;
    });
    if (tab) {
      if (activeWorker && CourseFlowMeetingBridgeCore.advancePageScope(activeWorker, tab.url)) {
        mediaContexts.clear();
        pendingRequests.clear();
      }
      const info = await inspectTab(tabId, firstInspection);
      firstInspection = false;
      if (info) return info;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error("PAGE_TIMEOUT");
}

function rememberRequest(details) {
  if (!activeWorker || details.tabId !== activeWorker.tabId || !urlAllowed(details.url) || !pageInitiator(details)) return;
  const headers = CourseFlowMeetingBridgeCore.sanitizeRequestHeaders(details.requestHeaders || []);
  pendingRequests.set(details.requestId, {
    taskId: activeWorker.taskId,
    generation: activeWorker.generation,
    url: requestKey(details.url),
    headers,
    updatedAt: Date.now()
  });
  pruneMemory();
}

function updateRequest(details) {
  const pending = pendingRequests.get(details.requestId);
  if (!pending || !activeWorker || pending.taskId !== activeWorker.taskId || pending.generation !== activeWorker.generation) return;
  const extra = CourseFlowMeetingBridgeCore.sanitizeRequestHeaders(details.requestHeaders || []);
  pending.headers = { ...pending.headers, ...extra };
  pending.updatedAt = Date.now();
}

function observeResponse(details) {
  const pending = pendingRequests.get(details.requestId);
  pendingRequests.delete(details.requestId);
  if (!pending || !activeWorker || pending.taskId !== activeWorker.taskId || pending.generation !== activeWorker.generation || details.statusCode >= 400) return;
  const contentType = (details.responseHeaders || [])
    .find((header) => header.name?.toLowerCase() === "content-type")?.value?.split(";", 1)[0].trim().toLowerCase() || "";
  if (CourseFlowMeetingBridgeCore.detectMediaKind(details.url, contentType) !== "mp4") return;
  const key = requestKey(details.url);
  const previous = mediaContexts.get(key);
  mediaContexts.set(key, {
    taskId: pending.taskId,
    generation: pending.generation,
    url: details.url,
    contentType,
    headers: { ...(previous?.headers || {}), ...pending.headers },
    updatedAt: Date.now()
  });
}

function pruneMemory() {
  const cutoff = Date.now() - 2 * 60 * 1000;
  for (const [key, item] of pendingRequests) if (item.updatedAt < cutoff) pendingRequests.delete(key);
  for (const [key, item] of mediaContexts) if (item.updatedAt < cutoff) mediaContexts.delete(key);
}

async function candidatesForTask(taskId) {
  pruneMemory();
  const candidates = [...mediaContexts.values()]
    .filter((item) => item.taskId === taskId && item.generation === activeWorker?.generation)
    .map((item) => ({ ...item, headers: { ...item.headers } }));
  for (const candidate of candidates) {
    if (candidate.headers.cookie) continue;
    try {
      const cookies = await chrome.cookies.getAll({ url: candidate.url });
      if (cookies.length) candidate.headers.cookie = cookies.map((cookie) => `${cookie.name}=${cookie.value}`).join("; ");
    } catch { /* 未取得 cookie 时保留网页请求捕获到的上下文。 */ }
  }
  return candidates;
}

async function waitForMedia(tabId, task) {
  const deadline = Date.now() + MEDIA_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const page = await inspectTab(tabId, false);
    const classification = CourseFlowMeetingBridgeCore.classifyPage(page || {});
    if (classification.kind === "LOGIN_REQUIRED" || classification.kind === "ACCESS_DENIED") throw new Error(classification.kind);
    const candidates = await candidatesForTask(task.task_id);
    if (candidates.length) return candidates;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return [];
}

async function submitResult(taskId, payload) {
  await bridgeFetch(`/tasks/${taskId}/result`, { method: "POST", body: JSON.stringify(payload) });
}

async function execute(task) {
  activeWorker = { taskId: task.task_id, tabId: -1, stage: "opening", pageScope: "", generation: 0 };
  mediaContexts.clear();
  pendingRequests.clear();
  const deps = {
    updateStage: async (taskId, stage) => {
      if (activeWorker) activeWorker.stage = stage;
      await bridgeFetch(`/tasks/${taskId}/progress`, { method: "POST", body: JSON.stringify({ stage }) });
    },
    openTab: async (url, options) => {
      const tab = await chrome.tabs.create({ url, active: options.active });
      activeWorker.tabId = tab.id;
      activeWorker.stage = "waiting_page";
      return tab;
    },
    waitForPage: async (tabId) => {
      activeWorker.stage = "waiting_page";
      return waitForPage(tabId);
    },
    waitForMedia: async (tabId, claimedTask) => {
      activeWorker.stage = "waiting_media";
      return waitForMedia(tabId, claimedTask);
    },
    submitResult: async (taskId, payload) => {
      activeWorker.stage = "submitting";
      await submitResult(taskId, payload);
    },
    closeTab: async (tabId) => {
      try { await chrome.tabs.remove(tabId); } catch { /* 用户或浏览器可能已关闭 worker tab。 */ }
    },
    clearTaskContext: () => {
      mediaContexts.clear();
      pendingRequests.clear();
      activeWorker = null;
    }
  };
  const result = await CourseFlowMeetingBridgeCore.executeTask(task, deps);
  workerStage = result.status === "success" ? "done" : "failed";
}

async function pollAndRun() {
  if (busy) {
    try { await heartbeat(); } catch { /* 下一次 tick 自动重连。 */ }
    return;
  }
  busy = true;
  try {
    await heartbeat();
    workerStage = "claiming";
    await CourseFlowMeetingBridgeCore.drainQueue(readNextTask, async (task) => {
      await execute(task);
      await heartbeat();
    });
    workerStage = "idle";
  } catch { /* 不把错误、任务地址或敏感请求上下文写入控制台。下次 tick 自动重连。 */ }
  finally { busy = false; }
}

function schedulePolling() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => { void pollAndRun(); }, HEARTBEAT_INTERVAL_MS);
  void pollAndRun();
}

chrome.webRequest.onBeforeSendHeaders.addListener(
  rememberRequest,
  { urls: ["https://*.tencent.com/*", "https://*.qcloud.com/*", "https://*.myqcloud.com/*", "https://*.tencent-cloud.net/*", "https://*.tencentcs.com/*"] },
  ["requestHeaders", "extraHeaders"]
);
chrome.webRequest.onSendHeaders.addListener(
  updateRequest,
  { urls: ["https://*.tencent.com/*", "https://*.qcloud.com/*", "https://*.myqcloud.com/*", "https://*.tencent-cloud.net/*", "https://*.tencentcs.com/*"] },
  ["requestHeaders", "extraHeaders"]
);
chrome.webRequest.onHeadersReceived.addListener(
  observeResponse,
  { urls: ["https://*.tencent.com/*", "https://*.qcloud.com/*", "https://*.myqcloud.com/*", "https://*.tencent-cloud.net/*", "https://*.tencentcs.com/*"] },
  ["responseHeaders"]
);
chrome.webRequest.onCompleted.addListener(
  (details) => pendingRequests.delete(details.requestId),
  { urls: ["https://*.tencent.com/*", "https://*.qcloud.com/*", "https://*.myqcloud.com/*", "https://*.tencent-cloud.net/*", "https://*.tencentcs.com/*"] }
);
chrome.webRequest.onErrorOccurred.addListener(
  (details) => pendingRequests.delete(details.requestId),
  { urls: ["https://*.tencent.com/*", "https://*.qcloud.com/*", "https://*.myqcloud.com/*", "https://*.tencent-cloud.net/*", "https://*.tencentcs.com/*"] }
);

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url && activeWorker && activeWorker.tabId === tabId && CourseFlowMeetingBridgeCore.advancePageScope(activeWorker, changeInfo.url)) {
    mediaContexts.clear();
    pendingRequests.clear();
  }
});

chrome.alarms.create("courseflow-meeting-bridge", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "courseflow-meeting-bridge") void pollAndRun();
});
chrome.runtime.onStartup.addListener(schedulePolling);
chrome.runtime.onInstalled.addListener(schedulePolling);
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "bridgePopupStatus") {
    fetch(`${API_BASE}/status`, { cache: "no-store" })
      .then((response) => response.ok ? response.json() : { connected: false })
      .then((status) => sendResponse({ ...status, workerActive: Boolean(activeWorker), stage: activeWorker?.stage || workerStage }))
      .catch(() => sendResponse({ connected: false, workerActive: Boolean(activeWorker), stage: activeWorker?.stage || workerStage }));
    return true;
  }
  return false;
});
schedulePolling();

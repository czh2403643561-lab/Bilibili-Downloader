(() => {
  const SAFE_HEADERS = new Set([
    "accept", "accept-language", "authorization", "cookie", "origin",
    "referer", "user-agent", "range"
  ]);
  const FAILURE_MESSAGES = {
    LOGIN_REQUIRED: "当前浏览器中的腾讯会议尚未登录，请先在浏览器中正常登录腾讯会议后重试。",
    ACCESS_DENIED: "当前账号无法访问该回放，或回放已不存在。",
    PAGE_TIMEOUT: "等待回放或视频资源超时，请确认回放可播放后重试。",
    MEDIA_NOT_FOUND: "已打开回放，但未发现 MP4 视频资源。",
    MULTIPLE_MEDIA: "发现多个 MP4 视频资源，无法安全确定唯一资源。",
    PAGE_ERROR: "回放页面解析失败，请稍后重试。"
  };

  function detectMediaKind(url, contentType = "") {
    const type = String(contentType).toLowerCase();
    let pathname = "";
    try { pathname = new URL(url).pathname.toLowerCase(); } catch { pathname = String(url).toLowerCase(); }
    if (type.includes("video/mp4") || type.includes("application/mp4") || /\.mp4(?:$|\/)/u.test(pathname)) return "mp4";
    if (type.includes("application/vnd.apple.mpegurl") || type.includes("application/x-mpegurl") || /\.m3u8(?:$|\/)/u.test(pathname)) return "hls";
    if (type.includes("application/dash+xml") || /\.mpd(?:$|\/)/u.test(pathname)) return "dash";
    return "";
  }

  function sanitizeRequestHeaders(headers = []) {
    const result = {};
    for (const header of headers) {
      const name = String(header?.name || "").trim().toLowerCase();
      const value = header?.value;
      if (!SAFE_HEADERS.has(name) || typeof value !== "string" || !value || value.length > 4096) continue;
      if (/[\r\n]/u.test(value)) continue;
      result[name] = value;
    }
    return result;
  }

  function mediaIdentity(value) {
    try {
      const url = new URL(value);
      return `${url.origin}${url.pathname}`;
    } catch { return ""; }
  }

  function advancePageScope(worker, value) {
    let next = "";
    try {
      const url = new URL(value);
      next = `${url.origin}${url.pathname}`;
    } catch { return false; }
    if (worker.pageScope === next) return false;
    worker.pageScope = next;
    worker.generation = (Number(worker.generation) || 0) + 1;
    return true;
  }

  function selectUniqueMp4Candidates(candidates = []) {
    const byIdentity = new Map();
    for (const candidate of candidates) {
      if (!candidate || detectMediaKind(candidate.url, candidate.contentType) !== "mp4") continue;
      const identity = mediaIdentity(candidate.url);
      if (!identity) continue;
      const previous = byIdentity.get(identity);
      const headers = sanitizeRequestHeaders(Object.entries(candidate.headers || {}).map(([name, value]) => ({ name, value })));
      byIdentity.set(identity, { ...candidate, headers: { ...(previous?.headers || {}), ...headers } });
    }
    return [...byIdentity.values()];
  }

  function classifyPage(info = {}) {
    let parsed;
    try { parsed = new URL(info.url); } catch { return { kind: "PAGE_ERROR" }; }
    const host = parsed.hostname.toLowerCase();
    const path = parsed.pathname.toLowerCase();
    if (host === "meeting.tencent.com" && /\/(?:login(?:\.html)?|signin|passport)(?:\/|$)/u.test(path)) {
      return { kind: "LOGIN_REQUIRED" };
    }
    if (info.loginRequired) return { kind: "LOGIN_REQUIRED" };
    if (info.accessDenied) return { kind: "ACCESS_DENIED" };
    if (host !== "meeting.tencent.com" || !/^\/(?:cw|crm)\//u.test(path)) return { kind: "PAGE_ERROR" };
    return { kind: "RECORDING_PAGE" };
  }

  function failure(code) {
    const safeCode = Object.hasOwn(FAILURE_MESSAGES, code) ? code : "PAGE_ERROR";
    return { status: "failed", code: safeCode, message: FAILURE_MESSAGES[safeCode] };
  }

  async function executeTask(task, dependencies) {
    let tab = null;
    let payload;
    try {
      if (dependencies.updateStage) await dependencies.updateStage(task.task_id, "opening");
      tab = await dependencies.openTab(task.url, { active: false });
      if (dependencies.updateStage) await dependencies.updateStage(task.task_id, "waiting_page");
      const page = await dependencies.waitForPage(tab.id, task);
      const classification = classifyPage(page);
      if (classification.kind !== "RECORDING_PAGE") throw new Error(classification.kind);
      if (dependencies.updateStage) await dependencies.updateStage(task.task_id, "waiting_media");
      const candidates = selectUniqueMp4Candidates(await dependencies.waitForMedia(tab.id, task));
      if (!candidates.length) throw new Error("MEDIA_NOT_FOUND");
      if (candidates.length !== 1) throw new Error("MULTIPLE_MEDIA");
      const candidate = candidates[0];
      payload = {
        status: "success",
        title: String(page.title || "").trim().slice(0, 200),
        duration_seconds: Number.isFinite(page.durationSeconds) && page.durationSeconds > 0 ? Math.floor(page.durationSeconds) : null,
        media: { url: candidate.url, headers: candidate.headers }
      };
    } catch (error) {
      const code = FAILURE_MESSAGES[error?.message] ? error.message : "PAGE_ERROR";
      payload = failure(code);
    }

    try {
      if (dependencies.updateStage) await dependencies.updateStage(task.task_id, "submitting");
      await dependencies.submitResult(task.task_id, payload);
    } finally {
      try { if (tab) await dependencies.closeTab(tab.id); } finally { dependencies.clearTaskContext(task.task_id); }
    }
    return payload;
  }

  async function drainQueue(readNext, execute) {
    let task = await readNext();
    while (task) {
      await execute(task);
      task = await readNext();
    }
  }

  const api = { detectMediaKind, sanitizeRequestHeaders, selectUniqueMp4Candidates, classifyPage, advancePageScope, failure, executeTask, drainQueue };
  globalThis.CourseFlowMeetingBridgeCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();

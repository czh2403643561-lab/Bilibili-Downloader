function isVisible(element) {
  if (!element || element.hidden || element.getAttribute("aria-hidden") === "true") return false;
  const style = getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
}

function visibleLoginSurface() {
  const candidates = document.querySelectorAll(
    '[role="dialog"], [class*="login" i], [class*="qrcode" i], [class*="qr-code" i]'
  );
  for (const element of candidates) {
    if (!isVisible(element)) continue;
    const text = (element.innerText || element.getAttribute("aria-label") || "").slice(0, 500);
    if (/(扫码|微信).{0,12}登录|登录.{0,12}(扫码|微信)|请先登录/iu.test(text)) return true;
  }
  return false;
}

function visibleAccessDenied() {
  const snippets = [];
  for (const element of document.querySelectorAll('[role="alert"], [class*="toast"], [class*="error"], [class*="empty"]')) {
    if (isVisible(element)) snippets.push((element.innerText || "").slice(0, 300));
  }
  return /无权限|没有权限|无权访问|回放不存在|录制不存在|已删除|已过期/iu.test(snippets.join(" "));
}

function inspectRecordingPage(prepare = false) {
  const loginRoute = /\/(?:login(?:\.html)?|signin|passport)(?:\/|$)/iu.test(location.pathname);
  const loginRequired = loginRoute || visibleLoginSurface();
  const videos = [...document.querySelectorAll("video")];
  if (prepare && !loginRequired) {
    for (const video of videos) {
      video.muted = true;
      video.preload = "auto";
      try {
        const playPromise = video.play();
        if (playPromise && typeof playPromise.catch === "function") playPromise.catch(() => undefined);
      } catch { /* 浏览器可能阻止自动播放；网络请求仍由正常页面加载触发。 */ }
    }
  }
  const duration = videos.map((video) => video.duration).find((value) => Number.isFinite(value) && value > 0);
  const title = globalThis.CourseFlowMeetingTitle
    ? globalThis.CourseFlowMeetingTitle.normalizeRecordingTitle(
      globalThis.CourseFlowMeetingTitle.cleanDocumentTitle(document.title)
    )
    : String(document.title || "").trim();
  return {
    url: location.href,
    title,
    durationSeconds: Number.isFinite(duration) ? duration : null,
    loginRequired,
    accessDenied: visibleAccessDenied()
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "inspectRecordingPage") {
    sendResponse(inspectRecordingPage(Boolean(message.prepare)));
    return false;
  }
  return false;
});

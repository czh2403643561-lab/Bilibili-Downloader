(() => {
  const GENERIC_TITLES = new Set([
    "腾讯会议", "tencent meeting", "会议", "录制文件", "回放", "当前页面", "media",
    "纪要", "时间轴", "逐字稿", "字幕", "聊天", "成员", "详情"
  ]);

  function collapseTitleWhitespace(value) {
    return String(value || "").replace(/\s+/gu, " ").trim();
  }

  function cleanDocumentTitle(value) {
    return collapseTitleWhitespace(value)
      .replace(/\s*[-|｜—–]\s*(?:腾讯会议|tencent meeting).*$/iu, "")
      .replace(/^(?:腾讯会议|tencent meeting)\s*[-|｜—–]\s*/iu, "");
  }

  function normalizeRecordingTitle(value) {
    const title = Array.from(collapseTitleWhitespace(value)).slice(0, 200).join("");
    const lowered = title.toLocaleLowerCase();
    if (!title || GENERIC_TITLES.has(lowered)) return "";
    if (/^(?:纪要|时间轴|逐字稿|字幕|聊天|成员|详情)(?:\s|$)/u.test(title)) return "";
    if (/^[\d\s._:/：-]+$/u.test(title) && /\d/u.test(title)) return "";
    if (/^(?:meeting|会议)[\s#_-]*\d+$/iu.test(title)) return "";
    if (/^\d{4}[年./-]\d{1,2}(?:[月./-]\d{1,2}(?:日)?)?$/u.test(title)) return "";
    return title;
  }

  globalThis.CourseFlowMeetingTitle = { cleanDocumentTitle, normalizeRecordingTitle };
})();

async function refresh() {
  const target = document.querySelector("#connection");
  const details = document.querySelector("#details");
  const diagnostic = document.querySelector("#diagnostic");
  try {
    const status = await chrome.runtime.sendMessage({ type: "bridgePopupStatus" });
    target.textContent = status?.connected ? "● CourseFlow 已连接" : `○ ${status?.message || "后台桥接尚未启动。"}`;
    target.className = `status${status?.connected ? " ready" : " problem"}`;
    details.textContent = `版本 ${status?.version || "0.1.0"} · ${status?.browser === "edge" ? "Edge" : "Chrome"} · ${status?.connected ? "浏览器桥接正常" : status?.last_success_relative || "等待连接"}`;
    diagnostic.textContent = `状态：${status?.state || "unknown"}${status?.http_status ? ` · HTTP ${status.http_status}` : ""} · 扩展 ID：${status?.extension_id || "未知"} · Worker：${status?.workerActive ? status.stage : "空闲"}`;
  } catch {
    target.textContent = "○ 后台桥接尚未启动";
    target.className = "status problem";
    details.textContent = "请在扩展管理页确认扩展已启用。";
  }
}

document.querySelector("#reconnect").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在重新连接…";
  try { await chrome.runtime.sendMessage({ type: "bridgeReconnect" }); } catch { /* 页面下次刷新会显示后台错误状态。 */ }
  await refresh();
  button.disabled = false;
  button.textContent = "重新连接";
});

document.querySelector("#open-courseflow").addEventListener("click", () => {
  void chrome.tabs.create({ url: "http://127.0.0.1:23666/", active: true });
});
void refresh();
setInterval(refresh, 2500);

async function refresh() {
  const target = document.querySelector("#connection");
  try {
    const status = await chrome.runtime.sendMessage({ type: "bridgePopupStatus" });
    target.textContent = status?.connected ? "● CourseFlow 已连接" : "○ CourseFlow 未启动";
    target.className = `status${status?.connected ? " ready" : ""}`;
  } catch {
    target.textContent = "○ CourseFlow 未启动";
    target.className = "status";
  }
}

document.querySelector("#open-courseflow").addEventListener("click", () => {
  void chrome.tabs.create({ url: "http://127.0.0.1:23666/", active: true });
});
void refresh();
setInterval(refresh, 2500);

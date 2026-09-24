const assert = require("node:assert/strict");
const fs = require("node:fs");
const crypto = require("node:crypto");
const path = require("node:path");
const test = require("node:test");
const core = require("../bridge_core.js");

test("manifest is MV3 with scoped permissions and no legacy capabilities", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"));
  assert.equal(manifest.manifest_version, 3);
  assert.ok(!manifest.host_permissions.includes("<all_urls>"));
  for (const forbidden of ["nativeMessaging", "downloads", "power", "sidePanel"]) {
    assert.ok(!manifest.permissions.includes(forbidden));
  }
  assert.ok(manifest.permissions.includes("webRequest"));
  assert.ok(manifest.host_permissions.includes("https://meeting.tencent.com/*"));
  assert.ok(manifest.host_permissions.includes("http://127.0.0.1/*"));
  const extensionId = [...crypto.createHash("sha256").update(Buffer.from(manifest.key, "base64")).digest("hex").slice(0, 32)]
    .map((digit) => String.fromCharCode(97 + Number.parseInt(digit, 16))).join("");
  const bridgeSource = fs.readFileSync(path.join(__dirname, "..", "..", "meeting_bridge.py"), "utf8");
  assert.match(bridgeSource, new RegExp(`BRIDGE_EXTENSION_ID = \\"${extensionId}\\"`));
});

test("detects MP4 from the URL or response content type", () => {
  assert.equal(core.detectMediaKind("https://cdn.qcloud.com/a.mp4?sign=x"), "mp4");
  assert.equal(core.detectMediaKind("https://cdn.qcloud.com/opaque", "video/mp4"), "mp4");
  assert.equal(core.detectMediaKind("https://cdn.qcloud.com/playlist.m3u8"), "hls");
});

test("request context only keeps known bounded headers", () => {
  const headers = core.sanitizeRequestHeaders([
    { name: "Cookie", value: "session=private" },
    { name: "Referer", value: "https://meeting.tencent.com/cw/a" },
    { name: "X-Unneeded", value: "drop" },
    { name: "Authorization", value: "line1\nline2" },
    { name: "Range", value: "bytes=0-100" },
  ]);
  assert.deepEqual(headers, {
    cookie: "session=private",
    referer: "https://meeting.tencent.com/cw/a",
    range: "bytes=0-100",
  });
});

test("classifies explicit login UI and allowed recording pages", () => {
  assert.equal(core.classifyPage({ url: "https://meeting.tencent.com/login.html" }).kind, "LOGIN_REQUIRED");
  assert.equal(core.classifyPage({ url: "https://meeting.tencent.com/cw/rec", loginRequired: true }).kind, "LOGIN_REQUIRED");
  assert.equal(core.classifyPage({ url: "https://meeting.tencent.com/cw/rec" }).kind, "RECORDING_PAGE");
  assert.equal(core.classifyPage({ url: "https://evil.example/login" }).kind, "PAGE_ERROR");
});

test("page scope generations isolate navigation changes but ignore query-only changes", () => {
  const worker = { pageScope: "", generation: 0 };
  assert.equal(core.advancePageScope(worker, "https://meeting.tencent.com/cw/one?a=1"), true);
  assert.equal(worker.generation, 1);
  assert.equal(core.advancePageScope(worker, "https://meeting.tencent.com/cw/one?a=2"), false);
  assert.equal(core.advancePageScope(worker, "https://meeting.tencent.com/login.html"), true);
  assert.equal(worker.generation, 2);
});

test("queue executes tasks one at a time", async () => {
  const queue = [1, 2, 3];
  let concurrent = 0;
  let maxConcurrent = 0;
  await core.drainQueue(async () => queue.length ? queue.shift() : null, async () => {
    concurrent += 1;
    maxConcurrent = Math.max(maxConcurrent, concurrent);
    await new Promise((resolve) => setTimeout(resolve, 5));
    concurrent -= 1;
  });
  assert.equal(maxConcurrent, 1);
});

async function runTask(page, candidates) {
  const observed = { options: null, submitted: null, closed: false, cleared: false, stages: [] };
  const payload = await core.executeTask({ task_id: "task-1", url: "https://meeting.tencent.com/cw/rec" }, {
    async updateStage(_taskId, stage) { observed.stages.push(stage); },
    async openTab(url, options) { observed.options = { url, ...options }; return { id: 9 }; },
    async waitForPage() { return page; },
    async waitForMedia() { return candidates; },
    async submitResult(taskId, result) { observed.submitted = { taskId, result }; },
    async closeTab(tabId) { observed.closed = tabId === 9; },
    clearTaskContext(taskId) { observed.cleared = taskId === "task-1"; },
  });
  return { payload, observed };
}

test("opens worker tab inactive and always closes it", async () => {
  const { payload, observed } = await runTask(
    { url: "https://meeting.tencent.com/cw/rec", title: "测试课程", durationSeconds: 93 },
    [{ url: "https://cdn.qcloud.com/video.mp4?sign=secret", contentType: "video/mp4", headers: { referer: "https://meeting.tencent.com/cw/rec" } }],
  );
  assert.equal(observed.options.active, false);
  assert.equal(observed.closed, true);
  assert.equal(observed.cleared, true);
  assert.deepEqual(observed.stages, ["opening", "waiting_page", "waiting_media", "submitting"]);
  assert.equal(payload.status, "success");
  assert.equal(payload.title, "测试课程");
  assert.equal(observed.submitted.result.media.url, "https://cdn.qcloud.com/video.mp4?sign=secret");
});

test("reports LOGIN_REQUIRED, MEDIA_NOT_FOUND, and MULTIPLE_MEDIA distinctly", async () => {
  const login = await runTask({ url: "https://meeting.tencent.com/cw/rec", loginRequired: true }, []);
  assert.equal(login.payload.code, "LOGIN_REQUIRED");
  const missing = await runTask({ url: "https://meeting.tencent.com/cw/rec" }, []);
  assert.equal(missing.payload.code, "MEDIA_NOT_FOUND");
  const multiple = await runTask({ url: "https://meeting.tencent.com/cw/rec" }, [
    { url: "https://cdn.qcloud.com/a.mp4", contentType: "video/mp4", headers: {} },
    { url: "https://cdn.qcloud.com/b.mp4", contentType: "video/mp4", headers: {} },
  ]);
  assert.equal(multiple.payload.code, "MULTIPLE_MEDIA");
  for (const result of [login, missing, multiple]) assert.equal(result.observed.closed, true);
});

test("same signed media URL is deduplicated and successful result has required payload", async () => {
  const { payload } = await runTask({ url: "https://meeting.tencent.com/cw/rec", title: "课程" }, [
    { url: "https://cdn.qcloud.com/a.mp4?sig=one", contentType: "video/mp4", headers: { referer: "https://meeting.tencent.com/cw/rec" } },
    { url: "https://cdn.qcloud.com/a.mp4?sig=two", contentType: "video/mp4", headers: { cookie: "secret=1" } },
  ]);
  assert.equal(payload.status, "success");
  assert.ok(payload.media.url.includes("/a.mp4?sig="));
  assert.ok(payload.media.headers.cookie);
});

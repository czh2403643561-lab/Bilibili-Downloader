const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { createBridgeClient } = require("../bridge_client.js");

function response(status, value = {}) {
  return { status, ok: status >= 200 && status < 300, async json() { return value; } };
}

function client(fetchImpl) {
  return createBridgeClient({
    apiBase: "http://127.0.0.1:23666/api/meeting-bridge",
    extensionId: "abmendmhafmkmcjkniplndbajmhnanej",
    browser: "chrome",
    fetchImpl
  });
}

test("ensureToken sends the fixed extension id and records pairing rejection", async () => {
  let headers;
  const bridge = client(async (_url, options) => {
    headers = options.headers;
    return response(200, { token: "process-random-token" });
  });
  assert.equal(await bridge.ensureToken(), "process-random-token");
  assert.equal(new Headers(headers).get("X-CourseFlow-Extension-Id"), "abmendmhafmkmcjkniplndbajmhnanej");
  assert.equal(new Headers(headers).get("X-CourseFlow-Extension-Browser"), "chrome");
});

test("pair 403 is classified without exposing response details", async () => {
  const bridge = client(async () => response(403, { error: "not for logs" }));
  await assert.rejects(bridge.ensureToken(), /pair_failed/);
  assert.equal(bridge.getDiagnostic().state, "pair_failed");
  assert.equal(bridge.getDiagnostic().http_status, 403);
});

test("unreachable CourseFlow is distinguished from pairing failure", async () => {
  const bridge = client(async () => { throw new TypeError("network failure"); });
  await assert.rejects(bridge.ensureToken());
  assert.equal(bridge.getDiagnostic().state, "service_unreachable");
});

test("a failed bridge request with a healthy local service is diagnosed as CORS/bridge failure", async () => {
  const bridge = client(async (url) => {
    if (url.endsWith("/api/health")) return response(200, { online: true });
    throw new TypeError("CORS or preflight rejected");
  });
  await assert.rejects(bridge.ensureToken());
  assert.equal(bridge.getDiagnostic().state, "cors_failed");
});

test("heartbeat 403 causes token reset, re-pair, and one safe retry", async () => {
  let pairCount = 0;
  let heartbeatCount = 0;
  const bridge = client(async (url) => {
    if (url.endsWith("/pair")) return response(200, { token: `token-${++pairCount}` });
    heartbeatCount += 1;
    return response(heartbeatCount === 1 ? 403 : 200, { ok: true });
  });
  await bridge.heartbeat();
  assert.equal(pairCount, 2);
  assert.equal(heartbeatCount, 2);
  assert.equal(bridge.getDiagnostic().state, "connected");
});

test("reconnect clears cached token and pairs before heartbeat", async () => {
  const requests = [];
  const bridge = client(async (url) => {
    requests.push(url.endsWith("/pair") ? "pair" : "heartbeat");
    return url.endsWith("/pair") ? response(200, { token: `token-${requests.length}` }) : response(200, { ok: true });
  });
  await bridge.ensureToken();
  await bridge.reconnect();
  assert.deepEqual(requests, ["pair", "pair", "heartbeat"]);
  assert.equal(bridge.getDiagnostic().state, "connected");
});

test("diagnostic persistence only restores the safe state fields", () => {
  const bridge = client(async () => response(500));
  bridge.restoreDiagnostic({ state: "connected", http_status: 200, last_success_at: 1234, token: "must-not-restore" });
  assert.deepEqual(bridge.getDiagnostic(), {
    state: "connected", message: "浏览器桥接正常", http_status: 200, last_success_at: 1234
  });
});

test("alarm, startup and install drive polling independently of popup lifetime", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
  assert.match(source, /chrome\.alarms\.create\("courseflow-meeting-bridge", \{ periodInMinutes: 0\.5 \}\)/);
  assert.match(source, /chrome\.alarms\.onAlarm\.addListener[\s\S]*?void pollAndRun\(\)/);
  assert.match(source, /chrome\.runtime\.onStartup\.addListener\(schedulePolling\)/);
  assert.match(source, /chrome\.runtime\.onInstalled\.addListener\(schedulePolling\)/);
  assert.match(source, /chrome\.storage\.session\.set/);
  assert.doesNotMatch(source, /chrome\.storage\.local/);
});

test("popup exposes diagnosis and reconnect controls without revealing bridge token", () => {
  const popup = fs.readFileSync(path.join(__dirname, "..", "popup.js"), "utf8");
  const html = fs.readFileSync(path.join(__dirname, "..", "popup.html"), "utf8");
  assert.match(popup, /bridgePopupStatus/);
  assert.match(popup, /bridgeReconnect/);
  assert.match(popup, /status\?\.state/);
  assert.match(html, /重新连接/);
  assert.match(html, /诊断信息/);
  assert.doesNotMatch(popup, /bridgeToken|token\s*:/);
});

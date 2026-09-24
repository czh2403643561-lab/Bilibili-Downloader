(() => {
  const MESSAGES = {
    connected: "浏览器桥接正常",
    service_unreachable: "无法连接 CourseFlow，请确认 CourseFlow 已启动。",
    pair_failed: "桥接配对失败，请检查版本并点击重新连接。",
    auth_failed: "桥接鉴权失败，请重启 CourseFlow 与扩展。",
    cors_failed: "CourseFlow 已启动，但桥接请求被浏览器拒绝；请检查扩展来源并重新连接。",
    heartbeat_failed: "桥接心跳失败，正在等待后台重连。",
    unknown: "后台桥接尚未启动或需要重试。"
  };

  function createBridgeClient({ apiBase, extensionId, browser, fetchImpl = fetch, saveDiagnostic = async () => {}, now = Date.now }) {
    let token = "";
    let diagnostic = { state: "unknown", message: MESSAGES.unknown, http_status: null, last_success_at: null };

    function publish(state, httpStatus = null) {
      diagnostic = { state, message: MESSAGES[state] || MESSAGES.unknown, http_status: httpStatus, last_success_at: diagnostic.last_success_at };
      void saveDiagnostic({ ...diagnostic });
      return { ...diagnostic };
    }

    function identityHeaders() {
      return {
        "X-CourseFlow-Extension-Id": extensionId,
        "X-CourseFlow-Extension-Browser": browser
      };
    }

    async function courseFlowReachable() {
      try {
        const response = await fetchImpl(new URL("/api/health", apiBase).href, { cache: "no-store" });
        return response.ok;
      } catch { return false; }
    }

    async function ensureToken(force = false) {
      if (token && !force) return token;
      try {
        const response = await fetchImpl(`${apiBase}/pair`, { headers: identityHeaders(), cache: "no-store" });
        if (!response.ok) {
          publish("pair_failed", response.status);
          throw new Error("pair_failed");
        }
        const payload = await response.json();
        if (typeof payload.token !== "string" || !payload.token) {
          publish("pair_failed", response.status);
          throw new Error("pair_failed");
        }
        token = payload.token;
        return token;
      } catch (error) {
        if (error?.message !== "pair_failed") {
          publish(await courseFlowReachable() ? "cors_failed" : "service_unreachable");
        }
        throw error;
      }
    }

    async function request(path, options = {}, retryAuth = true) {
      const currentToken = await ensureToken();
      const headers = new Headers(options.headers || {});
      Object.entries(identityHeaders()).forEach(([key, value]) => headers.set(key, value));
      headers.set("X-CourseFlow-Bridge-Token", currentToken);
      if (options.body) headers.set("Content-Type", "application/json");
      let response;
      try {
        response = await fetchImpl(`${apiBase}${path}`, { ...options, headers, cache: "no-store" });
      } catch (error) {
        publish(await courseFlowReachable() ? "cors_failed" : "service_unreachable");
        throw error;
      }
      if (response.status === 403) {
        token = "";
        publish("auth_failed", response.status);
        if (retryAuth) {
          await ensureToken(true);
          return request(path, options, false);
        }
      }
      if (response.status === 204) return null;
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (response.status !== 403) publish("heartbeat_failed", response.status);
        throw new Error(`bridge HTTP ${response.status}`);
      }
      return payload;
    }

    async function heartbeat() {
      try {
        await request("/heartbeat", {
          method: "POST",
          body: JSON.stringify({ version: "0.1.0", browser })
        });
        diagnostic = {
          state: "connected", message: MESSAGES.connected, http_status: 200,
          last_success_at: now()
        };
        void saveDiagnostic({ ...diagnostic });
        return { ...diagnostic };
      } catch (error) {
        if (diagnostic.state === "connected") publish("heartbeat_failed");
        throw error;
      }
    }

    async function reconnect() {
      token = "";
      publish("unknown");
      try {
        await ensureToken(true);
        return await heartbeat();
      } catch {
        return { ...diagnostic };
      }
    }

    async function getStatus() {
      try {
        const response = await fetchImpl(`${apiBase}/status`, { headers: identityHeaders(), cache: "no-store" });
        if (!response.ok) {
          publish("heartbeat_failed", response.status);
          return { connected: false, ...diagnostic };
        }
        const status = await response.json();
        if (status.connected && diagnostic.state !== "connected") {
          diagnostic = { state: "connected", message: MESSAGES.connected, http_status: 200, last_success_at: diagnostic.last_success_at || now() };
          void saveDiagnostic({ ...diagnostic });
        } else if (!status.connected && ["connected", "service_unreachable", "heartbeat_failed"].includes(diagnostic.state)) {
          publish("unknown");
        }
        return { ...status, ...diagnostic, connected: Boolean(status.connected && ["connected", "unknown"].includes(diagnostic.state)) };
      } catch {
        publish("service_unreachable");
        return { connected: false, ...diagnostic };
      }
    }

    function restoreDiagnostic(value) {
      if (!value || !Object.hasOwn(MESSAGES, value.state)) return;
      diagnostic = {
        state: value.state,
        message: MESSAGES[value.state],
        http_status: Number.isInteger(value.http_status) ? value.http_status : null,
        last_success_at: Number.isFinite(value.last_success_at) ? value.last_success_at : null
      };
    }

    return {
      ensureToken, request, heartbeat, reconnect, getStatus,
      getDiagnostic: () => ({ ...diagnostic }), restoreDiagnostic,
      reportUnexpectedFailure: () => diagnostic.state === "connected" ? publish("unknown") : { ...diagnostic }
    };
  }

  const api = { createBridgeClient, messages: MESSAGES };
  globalThis.CourseFlowBridgeClient = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();

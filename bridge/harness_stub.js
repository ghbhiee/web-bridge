// ---- harness stubs (everything else below is the real panel) ----
const FIXTURE = __FIXTURE__;
const TAB_URL = __TAB_URL__;
window.__calls = [];
window.chrome = {
  tabs: {
    query: async () => [{ id: 1, url: TAB_URL, title: "harness tab" }],
    update() {}, onActivated: { addListener() {} }, onUpdated: { addListener() {} },
  },
  runtime: { reload() {}, sendMessage: async () => ({ ok: true, registered: 0 }) },
  // backed by localStorage, not a plain object: chrome.storage.local survives a
  // panel close, so restore-after-reload has to be testable here too
  storage: { local: {
    async get(k) { const v = localStorage.getItem("stub:" + k); return v === null ? {} : { [k]: JSON.parse(v) }; },
    async set(o) { for (const [k, v] of Object.entries(o)) localStorage.setItem("stub:" + k, JSON.stringify(v)); },
    async remove(k) { localStorage.removeItem("stub:" + k); },
  } },
};
async function api(path, opts = {}) {
  window.__calls.push({ path, body: opts.body ? JSON.parse(opts.body) : null });
  if (path === "/health") return { ok: true, extension_connected: true, version: "harness" };
  if (path === "/agents") return { default: "claude",
    runners: { claude: { label: "Claude Code", available: true, enabled: true },
               codex: { label: "Codex", available: true, enabled: true } } };
  if (path.startsWith("/journal")) return { matches: [
    { summary: "抓取列表标题", runs: 4, ok_runs: 3, last: "2026-08-25T01:00:00",
      code: "return [...document.querySelectorAll('h3')].map(e=>e.textContent)" }] };
  if (path === "/tabs") return { tabs: [{ id: 1, url: TAB_URL, title: "harness tab" }] };
  if (path.startsWith("/capabilities")) return FIXTURE;
  if (path.startsWith("/capability/")) {
    if ((opts.method || "GET") === "GET") return { ok: true, source: "return 1", capability: {} };
    return { ok: true, result: { __echo_params: JSON.parse(opts.body || "{}").params } };
  }
  if (path.startsWith("/exec")) return { ok: true, result: "harness exec result" };
  if (path.startsWith("/agent/run/")) {
    // a run that is still going, so reattach has something to follow
    return { ok: true, id: "harness-run", done: false, events: [] };
  }
  throw new Error("unexpected " + path);
}
// streaming agent replies: hand back a canned NDJSON body
const _fetch = window.fetch;
window.fetch = async (u, opts) => {
  const path = String(u).replace(/^https?:\/\/[^/]+/, "");
  if (path === "/agent/ask") {
    window.__calls.push({ path, body: JSON.parse(opts.body) });
    const lines = [
      { type: "start", agent: "claude" },
      { type: "tool", name: "mcp__web-bridge__web_exec",
        input: { code: "return document.title;", url: "x.com" } },
      { type: "text", text: "这是 harness 里的模拟回答。\n\n```js\nreturn document.title;\n```" },
      { type: "done", session_id: "harness-session" },
      { type: "end" },
    ].map((e) => JSON.stringify(e)).join("\n");
    return new Response(new Blob([lines]), { status: 200, headers: { "X-Run-Id": "harness" } });
  }
  if (path.startsWith("/agent/run/") && path.includes("follow=true")) {
    window.__calls.push({ path });
    const lines = [
      { type: "text", text: "（重新接上）这是面板关闭期间 agent 继续产出的答案。" },
      { type: "done" }, { type: "end" },
    ].map((e) => JSON.stringify(e)).join("\n");
    return new Response(new Blob([lines]), { status: 200 });
  }
  if (path === "/health") return new Response(JSON.stringify({ ok: true, extension_connected: true, version: "harness" }));
  return _fetch(u, opts);
};

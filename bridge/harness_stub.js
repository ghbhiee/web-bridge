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
  if (path.startsWith("/user-scripts/export")) return { kind: "web-bridge/user-scripts", version: 1, scripts: [{ id: "u_demo", name: "隐藏侧边栏", code: "return 1" }] };
  if (path.startsWith("/user-scripts/import")) return { ok: true, added: ["新脚本"], replaced: [], renamed: ["旧的（导入）"], skipped: [] };
  if (path.startsWith("/capabilities/export")) return { kind: "web-bridge/capabilities", version: 1, capabilities: [{ id: "extract-tables", source: "/* x */" }] };
  if (path.startsWith("/capabilities/import")) return { ok: true, added: ["新能力"], replaced: [], skipped: ["extract-tables"] };
  if (path.startsWith("/user-scripts")) return { ok: true, total: 1, scripts: [
    { id: "u_demo", name: "隐藏侧边栏", code: "document.querySelector('.sidebar')?.remove();\nreturn {hidden:true};",
      matches: ["example.com"], autorun: true,
      note: "隐藏左侧导航栏，正文占满宽度\n· 2026-08-26 增加了折叠动画",
      updated: new Date(Date.now() - 9e5).toISOString().slice(0, 19),
      created_by: "claude", updated_by: "codex", revisions: 2 }] };
  if (path.startsWith("/user-script/")) {
    if ((opts.method || "GET") === "POST" && path.endsWith("/run"))
      return { ok: true, script: "隐藏侧边栏", result: { hidden: true } };
    const body = opts.body ? JSON.parse(opts.body) : {};
    const isNew = path.includes("/new");
    const id = isNew ? "u_new" + (window.__savedCount = (window.__savedCount || 0) + 1) : path.split("/")[2];
    return { ok: true, script: { id, name: body.name || "已保存的脚本",
                                 code: body.code, matches: body.matches || ["*"],
                                 autorun: body.autorun ?? false } };
  }
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
      { type: "text", text: "这是 harness 里的模拟回答。\n\n```js\n" +
        Array.from({length: 30}, (_, i) => `const line${i} = ${i};`).join("\n") +
        "\nreturn document.title;\n```" },
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

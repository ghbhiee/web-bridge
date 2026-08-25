#!/usr/bin/env python3
"""Render the extension side panel into a plain web page for testing.

Chrome forbids scripting another extension's pages, so the side panel is the one part
of web-bridge that cannot be driven through the bridge itself. This builds an
equivalent page — the REAL panel.html and panel.js, with only the three
extension-only seams stubbed:

    ../config.js import   → inline constants
    chrome.tabs / runtime → a fake current tab
    api()                 → the live capability catalog, captured to a fixture

so rendering, the parameter form, and readForm() can be exercised in any browser
(and by an agent). Runs are echoed back instead of hitting the bridge, and every
call is recorded on `window.__calls` for assertions.

    python3 bridge/popup_harness.py [--url https://example.com/]
    # then serve .harness/ and open harness.html

The bridge must be running (the catalog is fetched from it).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402


def catalog(url: str) -> dict:
    req = urllib.request.Request(
        f"{config.base_url()}/capabilities?url={urllib.parse.quote(url)}")
    req.add_header("Authorization", f"Bearer {config.TOKEN}")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def build(url: str, out_dir: Path) -> Path:
    html = (ROOT / "extension/sidepanel/panel.html").read_text(encoding="utf-8")
    js = (ROOT / "extension/sidepanel/panel.js").read_text(encoding="utf-8")
    caps = catalog(url)

    js = js.replace(
        'import { BRIDGE_WS, BRIDGE_TOKEN } from "../config.js";',
        'const BRIDGE_WS = "ws://127.0.0.1:8790/ws/ext", BRIDGE_TOKEN = "stub";')
    stub = """
// ---- harness stubs (everything else below is the real panel) ----
const FIXTURE = %s;
const TAB_URL = %s;
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
      { type: "tool", name: "Read", input: { file: "x" } },
      { type: "text", text: "这是 harness 里的模拟回答。\\n\\n```js\\nreturn document.title;\\n```" },
      { type: "done", session_id: "harness-session" },
      { type: "end" },
    ].map((e) => JSON.stringify(e)).join("\\n");
    return new Response(new Blob([lines]), { status: 200, headers: { "X-Run-Id": "harness" } });
  }
  if (path.startsWith("/agent/run/") && path.includes("follow=true")) {
    window.__calls.push({ path });
    const lines = [
      { type: "text", text: "（重新接上）这是面板关闭期间 agent 继续产出的答案。" },
      { type: "done" }, { type: "end" },
    ].map((e) => JSON.stringify(e)).join("\\n");
    return new Response(new Blob([lines]), { status: 200 });
  }
  if (path === "/health") return new Response(JSON.stringify({ ok: true, extension_connected: true, version: "harness" }));
  return _fetch(u, opts);
};
""" % (json.dumps(caps, ensure_ascii=False), json.dumps(url))
    # lambda, not a plain string: re.sub expands escapes in the replacement, so a
    # literal \n in the stub's JS would become a real newline and break the script
    js = re.sub(r"async function api\(path, opts = \{\}\) \{.*?\n\}\n",
                lambda _m: stub, js, count=1, flags=re.S)

    html = html.replace('<script type="module" src="panel.js"></script>',
                        "<script type=\"module\">\n" + js + "\n</script>")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "harness.html"
    out.write_text(html, encoding="utf-8")
    # panel.html links panel.css relatively; copy it so the harness renders like
    # the real panel instead of unstyled markup
    (out_dir / "panel.css").write_text(
        (ROOT / "extension/sidepanel/panel.css").read_text(encoding="utf-8"), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="https://chatgpt.com/", help="pretend the popup opened on this page")
    ap.add_argument("--out", default=str(ROOT / ".harness"))
    a = ap.parse_args()
    try:
        out = build(a.url, Path(os.path.expanduser(a.out)))
    except Exception as e:  # noqa: BLE001
        print(f"生成失败（bridge 在跑吗？）: {e}", file=sys.stderr)
        return 1
    print(out)
    print("  python3 -m http.server 8791 --directory " + str(Path(a.out).expanduser()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

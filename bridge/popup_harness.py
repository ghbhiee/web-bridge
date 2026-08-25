#!/usr/bin/env python3
"""Render the extension popup into a plain web page for testing.

Chrome forbids scripting another extension's pages, so the popup is the one part
of web-bridge that cannot be driven through the bridge itself. This builds an
equivalent page — the REAL popup.html and popup.js, with only the three
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
    html = (ROOT / "extension/popup/popup.html").read_text(encoding="utf-8")
    js = (ROOT / "extension/popup/popup.js").read_text(encoding="utf-8")
    caps = catalog(url)

    js = js.replace(
        'import { BRIDGE_WS, BRIDGE_TOKEN } from "../config.js";',
        'const BRIDGE_WS = "ws://127.0.0.1:8790/ws/ext", BRIDGE_TOKEN = "stub";')
    stub = """
// ---- harness stubs (everything else below is the real popup) ----
const FIXTURE = %s;
const TAB_URL = %s;
window.__calls = [];
window.chrome = { tabs: { query: async () => [{ url: TAB_URL }] }, runtime: { reload() {} } };
async function api(path, opts = {}) {
  window.__calls.push({ path, body: opts.body ? JSON.parse(opts.body) : null });
  if (path === "/health") return { extension_connected: true };
  if (path.startsWith("/capabilities")) return FIXTURE;
  if (path.startsWith("/capability/")) return { ok: true, result: { __echo_params: JSON.parse(opts.body).params } };
  throw new Error("unexpected " + path);
}
""" % (json.dumps(caps, ensure_ascii=False), json.dumps(url))
    js = re.sub(r"async function api\(path, opts = \{\}\) \{.*?\n\}\n", stub, js, count=1, flags=re.S)

    html = html.replace('<script type="module" src="popup.js"></script>',
                        "<script type=\"module\">\n" + js + "\n</script>")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "harness.html"
    out.write_text(html, encoding="utf-8")
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

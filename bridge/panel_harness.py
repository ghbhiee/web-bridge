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
    # The stub lives in harness_stub.js, not in a Python string. Twice now, a
    # literal \n inside JS embedded here turned into a real newline (once via the
    # triple-quoted string, once via re.sub's replacement expansion) and produced
    # a harness that would not parse. A real file has no escaping layer to get
    # wrong; only two placeholders are substituted.
    stub = (HERE / "harness_stub.js").read_text(encoding="utf-8")
    stub = stub.replace("__FIXTURE__", json.dumps(caps, ensure_ascii=False))
    stub = stub.replace("__TAB_URL__", json.dumps(url))
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

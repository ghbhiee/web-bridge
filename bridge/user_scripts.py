"""User scripts — the page-beauty side of the panel.

Deliberately NOT capabilities. They look similar (both are JS injected into a
page) but they serve different people and must not be mixed:

  capabilities/   written BY the agent, FOR the agent. Carry parameter
                  declarations, kinds, descriptions the agent reasons over. The
                  user has no reason to read this code — they only want to know
                  which abilities exist.
  user-scripts/   written BY the user (pasted in, or drafted with the agent's
                  help). Their code is the whole point: the user reads it, edits
                  it, and owns it. No parameters, no discovery metadata.

Merging them — which an earlier design did — meant the user's script list was
buried in machine-facing entries, and the agent's capability discovery was
polluted with one-off page tweaks.

Storage is one JSON file rather than a directory of JS files: these are edited as
a set from one screen, they have no metadata header to parse, and a plain list
keeps ordering and timestamps trivial.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import config

STORE = Path(os.environ.get(
    "WEB_BRIDGE_USER_SCRIPTS", str(config.CONFIG_PATH.parent / "user-scripts.json")))


def _load() -> list[dict]:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(scripts: list[dict]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(scripts, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE)
    try:
        STORE.chmod(0o600)
    except OSError:
        pass


def all_scripts() -> list[dict]:
    return _load()


def get(script_id: str) -> Optional[dict]:
    return next((s for s in _load() if s["id"] == script_id), None)


def match_patterns(script: dict) -> list[str]:
    """Chrome match patterns for registration.

    Same conversion as capabilities: users type a host, userScripts.register
    demands a real pattern, and one bad pattern rejects the entire batch.
    """
    pats = []
    for p in script.get("matches") or ["*"]:
        p = p.strip()
        if not p:
            continue
        if p == "*":
            pats.append("*://*/*")
        elif p.startswith(("http://", "https://", "*://")):
            pats.append(p)
        elif "/" in p:
            host, _, rest = p.partition("/")
            pats.append(f"*://*.{host}/{rest}*")
        else:
            pats.append(f"*://*.{p}/*")
    return pats or ["*://*/*"]


def matches_url(script: dict, url: str) -> bool:
    if not url:
        return False
    for p in script.get("matches") or ["*"]:
        p = (p or "").strip()
        if p == "*":
            return True
        bare = re.sub(r"^\*://|^https?://|\*", "", p).strip("/")
        if bare and bare.split("/")[0] in url:
            return True
    return False


def for_url(url: str = "") -> list[dict]:
    scripts = _load()
    if not url:
        return scripts
    return [s for s in scripts if matches_url(s, url)]


def save(script: dict) -> dict:
    """Create or update. Returns the stored record.

    Fields left out of an update keep their stored value. This matters because
    updates arrive from more than one place: the chat's save button sends only
    the new code, and rebuilding the record from scratch there would silently
    switch off an autorun the user had turned on in the panel.
    """
    if not (script.get("code") or "").strip():
        raise ValueError("代码不能为空")
    scripts = _load()
    sid = script.get("id") or ("u_" + uuid.uuid4().hex[:8])
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    existing = next((s for s in scripts if s["id"] == sid), None)
    prev = existing or {}

    def keep(field, value, default):
        if value is None:
            return prev.get(field, default)
        return value

    record = {
        "id": sid,
        "name": (keep("name", script.get("name"), "") or "").strip() or prev.get("name") or "未命名脚本",
        "code": script["code"],
        "matches": keep("matches", script.get("matches"), None) or prev.get("matches") or ["*"],
        "autorun": bool(keep("autorun", script.get("autorun"), False)),
        "note": (keep("note", script.get("note"), "") or "").strip(),
        "created": prev.get("created", now),
        "updated": now,
    }
    if existing:
        scripts[scripts.index(existing)] = record
    else:
        scripts.append(record)
    _save(scripts)
    return record


def delete(script_id: str) -> bool:
    scripts = _load()
    left = [s for s in scripts if s["id"] != script_id]
    if len(left) == len(scripts):
        return False
    _save(left)
    return True


def set_autorun(script_id: str, on: bool) -> dict:
    s = get(script_id)
    if not s:
        raise KeyError(f"没有这个脚本：{script_id}")
    s["autorun"] = bool(on)
    return save(s)


def bookmarklet(script: dict) -> str:
    """Wrap a script as a `javascript:` URL.

    The stored code is a function body (it may `await`, it may `return`), so it
    is wrapped in an async IIFE exactly like the injected form — otherwise a
    top-level `return` is a syntax error the moment the bookmark is clicked.
    """
    body = (script.get("code") or "").strip()
    wrapped = "(async()=>{try{" + body + "}catch(e){alert('脚本出错: '+e.message)}})()"
    # encodeURIComponent-equivalent: a bookmark URL must survive %, #, ? and
    # newlines, which are exactly what page-scripts are full of
    from urllib.parse import quote
    return "javascript:" + quote(wrapped, safe="")


def bookmarklet_page(script: dict) -> str:
    """A tiny self-contained page whose link can be dragged to the bookmarks bar.

    Dragging is the only way to install a bookmarklet in Chrome — you cannot
    paste `javascript:` into the address bar and you cannot create it through an
    API. So the export has to be a page with a real anchor on it.
    """
    import html as _html
    href = bookmarklet(script)
    name = script.get("name") or "web-bridge 脚本"
    return f"""<!doctype html><meta charset="utf-8"><title>{_html.escape(name)} · 书签</title>
<style>
 body{{font:15px/1.7 -apple-system,"PingFang SC",sans-serif;max-width:720px;margin:48px auto;padding:0 20px;color:#1a1a1a}}
 h1{{font-size:20px;margin:0 0 6px}} .sub{{color:#6b7280;font-size:13px;margin-bottom:24px}}
 .drag{{display:inline-block;background:#2563eb;color:#fff;text-decoration:none;padding:10px 20px;
        border-radius:8px;font-weight:600;cursor:grab}}
 .steps{{background:#f6f7f9;border-radius:10px;padding:14px 18px;margin:22px 0;font-size:14px}}
 code{{background:#eef0f3;border-radius:4px;padding:1px 5px;font-size:13px}}
 pre{{background:#f6f7f9;border-radius:10px;padding:14px;overflow:auto;font-size:12px;line-height:1.5}}
</style>
<h1>{_html.escape(name)}</h1>
<div class="sub">web-bridge 导出的书签小工具 · 适用范围 {_html.escape(", ".join(script.get("matches") or ["*"]))}</div>
<a class="drag" href="{_html.escape(href, quote=True)}">↧ 把我拖到书签栏</a>
<div class="steps">
  <b>怎么用</b><br>
  1. 显示书签栏（Chrome: <code>⌘⇧B</code>）<br>
  2. 把上面的蓝色按钮<b>拖</b>到书签栏——不能复制粘贴，浏览器不允许手输 <code>javascript:</code><br>
  3. 到目标页面点一下这个书签，脚本就地执行<br>
  换台电脑就把这个 HTML 文件带过去，或者存进网盘再打开。
</div>
<details><summary>脚本源码</summary><pre>{_html.escape(script.get("code") or "")}</pre></details>
"""


def autorun_for_registration() -> list[dict]:
    """User scripts the extension should run on page load."""
    return [{"id": "u:" + s["id"], "title": s["name"], "matches": match_patterns(s),
             "code": s["code"], "args": {}, "kind": "user"}
            for s in _load() if s.get("autorun") and (s.get("code") or "").strip()]

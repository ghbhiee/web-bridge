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

    # The note is a running record, not a single value: on an update the new
    # summary is appended with its date, so the entry says what it can do now AND
    # what each round added. Without this an updated script kept describing only
    # its first version.
    note = (script.get("note") or "").strip()
    old_note = (prev.get("note") or "").strip()
    if note and old_note and note not in old_note:
        merged = old_note + f"\n· {now[:10]} {note}"
    else:
        merged = note or old_note

    record = {
        "id": sid,
        "name": (keep("name", script.get("name"), "") or "").strip() or prev.get("name") or "未命名脚本",
        "code": script["code"],
        "matches": keep("matches", script.get("matches"), None) or prev.get("matches") or ["*"],
        "autorun": bool(keep("autorun", script.get("autorun"), False)),
        "note": merged,
        "created": prev.get("created", now),
        "updated": now,
        # who made it, so a list of scripts says where each came from
        "created_by": prev.get("created_by") or script.get("by") or "",
        "updated_by": script.get("by") or prev.get("updated_by") or "",
        "revisions": int(prev.get("revisions", 0)) + (1 if existing else 0),
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
    """A self-contained page whose link can be dragged to the bookmarks bar.

    This file is meant to LEAVE this machine — mailed, put on a stick, dropped in
    a chat — and be opened on a computer with no extension, no bridge and no
    agent. So everything is inline: styles, the instructions, and the code
    itself (inside the anchor's href). It has to explain itself to someone who
    has never seen this project.

    Dragging is the only way to install a bookmarklet in Chrome: a typed or
    pasted `javascript:` URL is refused, and no API can create one.
    """
    import html as _html
    href = bookmarklet(script)
    name = script.get("name") or "web-bridge 脚本"
    where = ", ".join(script.get("matches") or ["*"])
    size = len(href)
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>{_html.escape(name)} · 书签小工具</title>
<style>
 :root{{color-scheme:light dark}}
 body{{font:15px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       max-width:680px;margin:0 auto;padding:48px 22px 80px;color:#1a1a1a;background:#fff}}
 @media (prefers-color-scheme:dark){{body{{background:#16181c;color:#e6e6e6}}
   .steps,pre{{background:#22262d !important}} code{{background:#2a2f37 !important}}
   .sub,.foot{{color:#9aa0a6 !important}}}}
 h1{{font-size:22px;margin:0 0 4px}}
 .sub{{color:#6b7280;font-size:13px;margin-bottom:28px}}
 .drop{{border:2px dashed #c7cbd1;border-radius:14px;padding:26px;text-align:center;margin-bottom:26px}}
 .drag{{display:inline-block;background:#2563eb;color:#fff;text-decoration:none;padding:12px 26px;
        border-radius:9px;font-weight:600;font-size:15px;cursor:grab}}
 .drag:active{{cursor:grabbing}}
 .hint{{color:#6b7280;font-size:12.5px;margin-top:12px}}
 .steps{{background:#f6f7f9;border-radius:12px;padding:16px 20px;margin:24px 0;font-size:14px}}
 .steps li{{margin:6px 0}}
 code{{background:#eef0f3;border-radius:4px;padding:1px 6px;font-size:13px}}
 pre{{background:#f6f7f9;border-radius:12px;padding:14px;overflow:auto;font-size:12px;line-height:1.55}}
 .foot{{color:#6b7280;font-size:12px;margin-top:36px;border-top:1px solid #e5e7eb;padding-top:14px}}
</style>
<h1>{_html.escape(name)}</h1>
<div class="sub">一个书签小工具 · 适用页面：{_html.escape(where)} · {size:,} 字符</div>

<div class="drop">
  <a class="drag" href="{_html.escape(href, quote=True)}">↧ 拖我到书签栏</a>
  <div class="hint">按住这个按钮，拖到浏览器的书签栏上松手</div>
</div>

<div class="steps">
  <b>怎么用（这台电脑不需要装任何东西）</b>
  <ol>
    <li>显示书签栏：Chrome / Edge 按 <code>⌘⇧B</code>（Windows 是 <code>Ctrl+Shift+B</code>）</li>
    <li>把上面的蓝色按钮<b>拖</b>到书签栏——不能复制粘贴，浏览器不允许手动输入
        <code>javascript:</code> 开头的书签</li>
    <li>打开目标网页，点一下书签栏上的它，脚本就在当前页面执行</li>
  </ol>
</div>

<details><summary>看看它做了什么（源码）</summary>
<pre>{_html.escape(script.get("code") or "")}</pre></details>

<div class="foot">这个文件是自包含的：直接双击打开就行，不联网、不依赖任何扩展或插件，
换电脑复制过去照样能用。由 web-bridge 导出。</div>
</html>
"""


def autorun_for_registration() -> list[dict]:
    """User scripts the extension should run on page load."""
    return [{"id": "u:" + s["id"], "title": s["name"], "matches": match_patterns(s),
             "code": s["code"], "args": {}, "kind": "user"}
            for s in _load() if s.get("autorun") and (s.get("code") or "").strip()]

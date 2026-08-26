"""Exec journal — the memory that turns one-off scripts into capabilities.

Every `/exec` and `/capability/{id}` call is appended to a greppable JSONL log.
Identical scripts are recognised by a normalised hash, counted per host, and once
a script has *worked* PROMOTE_AFTER times it is written into the capability
library automatically. Nothing depends on an agent remembering to save its work:
the flywheel turns on its own, and the agent is told about it in the response.

Files (chmod 600, next to the config):
    ~/.config/web-bridge/exec-log.jsonl    append-only, one JSON per line — grep this
    ~/.config/web-bridge/exec-index.json    per-script counters (fast, no full scan)

What is NOT stored: page data. Results are recorded by *shape* (type, keys,
size) only — the log is a record of what was run, not a copy of what was read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import config
import capabilities

STATE_DIR = Path(os.environ.get("WEB_BRIDGE_STATE", str(config.CONFIG_PATH.parent)))
LOG_PATH = STATE_DIR / "exec-log.jsonl"
INDEX_PATH = STATE_DIR / "exec-index.json"

MAX_CODE_CHARS = 6000
MAX_LOG_BYTES = 5 * 1024 * 1024
PROMOTE_AFTER = int(config.CFG.get("promote_after", 3))
AUTO_DIR = capabilities.CAP_DIR / "auto"


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
# string literals, longest-first so a quote inside another quote type is safe
_STRING_RE = re.compile(r"`(?:\\.|[^`\\])*`|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", re.S)


def normalise(code: str) -> str:
    """Same script modulo comments and formatting → same signature.

    An agent rarely reproduces a script byte-for-byte; it re-indents, adds a
    comment, breaks an object literal across lines. Hashing the raw text would
    treat every one of those as a brand-new script, so nothing would ever reach
    the promotion threshold and the library would never grow.

    String literals are pulled out FIRST and stand in as content hashes. Two
    reasons: stripping comments with a regex would otherwise eat the rest of a
    line at the `//` inside `"https://…"`, and collapsing whitespace would make
    `"div a"` and `"diva"` — different selectors — look like the same script.
    """
    literals: list[str] = []

    def stash(m: re.Match) -> str:
        literals.append(m.group(0))
        return f"\x00S{len(literals) - 1}\x00"

    protected = _STRING_RE.sub(stash, code or "")
    protected = _COMMENT_RE.sub(" ", protected)
    protected = re.sub(r"\s+", " ", protected)
    # formatting-only differences: whitespace next to punctuation carries no
    # meaning in JS ("{ a: 1 }" === "{a:1}"), but a space BETWEEN two word
    # characters does ("return x" is not "returnx"), so only the former goes
    protected = re.sub(r"\s*([^\w$\x00])\s*", r"\1", protected).strip()
    for i, lit in enumerate(literals):
        protected = protected.replace(f"\x00S{i}\x00",
                                      hashlib.md5(lit.encode("utf-8")).hexdigest()[:8])
    return protected


def signature(code: str) -> str:
    return hashlib.sha1(normalise(code).encode("utf-8")).hexdigest()[:12]


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    if m:
        return m.group(1)
    return (url or "").split("/")[0][:60]


def summarise(code: str) -> str:
    """A one-line label. The first comment wins — which is why the docs tell
    agents to start a one-off script with `// what this does`."""
    for line in (code or "").splitlines():
        line = line.strip()
        if line.startswith("//"):
            text = line.lstrip("/").strip()
            if text:
                return text[:120]
        if line and not line.startswith(("/*", "*")):
            break
    body = normalise(code)
    return (body[:100] + "…") if len(body) > 100 else body


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def _load_index() -> dict:
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_index(idx: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(INDEX_PATH)
    try:
        INDEX_PATH.chmod(0o600)
    except OSError:
        pass


def _append(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            LOG_PATH.replace(LOG_PATH.with_suffix(".jsonl.1"))
    except OSError:
        pass
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        LOG_PATH.chmod(0o600)
    except OSError:
        pass


def shape_of(result: Any) -> dict:
    """Describe a result without keeping it: the log records what ran, not what
    was read off the user's pages."""
    if result is None:
        return {"type": "null"}
    if isinstance(result, dict):
        return {"type": "object", "keys": list(result)[:15], "size": len(result)}
    if isinstance(result, list):
        return {"type": "array", "size": len(result),
                "item_keys": list(result[0])[:12] if result and isinstance(result[0], dict) else None}
    if isinstance(result, str):
        return {"type": "string", "size": len(result)}
    return {"type": type(result).__name__}


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def record(*, kind: str, code: str = "", args: Any = None, url: str = "",
           site: str = "", ok: bool = True, ms: int = 0, result: Any = None,
           error: str = "", capability: str = "") -> dict:
    """Append one call and update its counter. Returns what the caller should
    know: how many times this script has run, and whether it just got promoted."""
    sig = signature(code) if code else f"cap:{capability}"
    host = host_of(url) or site or "?"
    key = f"{host}|{sig}"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    idx = _load_index()
    slot = idx.get(key) or {"sig": sig, "host": host, "runs": 0, "ok_runs": 0,
                            "first": now, "kind": kind, "capability": capability or None}
    slot["runs"] += 1
    slot["ok_runs"] += 1 if ok else 0
    slot["last"] = now
    slot["summary"] = summarise(code) if code else capability
    if code:
        slot["code"] = code[:MAX_CODE_CHARS]
        if args:
            slot["args"] = args
    idx[key] = slot

    entry = {"t": now, "kind": kind, "host": host, "sig": sig, "url": url[:300],
             "ok": ok, "ms": ms, "runs": slot["runs"]}
    if capability:
        entry["capability"] = capability
    if code:
        entry["summary"] = slot["summary"]
        entry["code"] = code[:MAX_CODE_CHARS]
        if args:
            entry["args"] = args
    if ok:
        entry["result"] = shape_of(result)
    else:
        entry["error"] = (error or "")[:400]
    _append(entry)

    out = {"runs": slot["runs"], "ok_runs": slot["ok_runs"], "signature": sig}
    if kind == "exec" and ok:
        promoted = maybe_promote(slot)
        if promoted:
            slot["promoted_to"] = promoted
            out["promoted_to"] = promoted
            out["hint"] = (
                f"这段脚本在 {host} 上已成功跑了 {slot['ok_runs']} 次，已自动沉淀为能力 "
                f"'{promoted}'（capabilities/auto/）。下次直接 web_run_capability / wb run {promoted}。"
                f"给它起个像样的 id 和描述：web_save_capability 覆盖同名文件即可。")
        elif slot["ok_runs"] == PROMOTE_AFTER - 1:
            out["hint"] = (f"这段脚本在 {host} 上已成功跑了 {slot['ok_runs']} 次；"
                           f"再跑一次会被自动沉淀成能力。想自己命名就现在 web_save_capability。")
    _save_index(idx)
    return out


# --------------------------------------------------------------------------- #
# promotion — a repeated script becomes a real, discoverable capability
# --------------------------------------------------------------------------- #
def _params_from_args(args: Any) -> dict:
    if not isinstance(args, dict):
        return {}
    types = {str: "string", bool: "boolean", int: "number", float: "number",
             list: "array", dict: "object"}
    out = {}
    for k, v in list(args.items())[:12]:
        out[str(k)] = {"type": types.get(type(v), "any"),
                       "default": v if not isinstance(v, (dict, list)) else None,
                       "description": f"自动沉淀时记录的参数（上次的值：{json.dumps(v, ensure_ascii=False)[:60]}）"}
        if out[str(k)]["default"] is None:
            out[str(k)].pop("default")
    return out


def auto_id(host: str, sig: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:24]
    return f"auto-{slug}-{sig[:6]}"


def maybe_promote(slot: dict) -> Optional[str]:
    """Write a repeated script into the library. Deliberately automatic: relying
    on an agent to remember `save_capability` is exactly why the library never
    grew on its own."""
    if slot.get("promoted_to") or slot["ok_runs"] < PROMOTE_AFTER:
        return None
    code = slot.get("code") or ""
    if not code.strip():
        return None
    cap_id = auto_id(slot["host"], slot["sig"])
    if capabilities.get(cap_id):
        return cap_id
    meta = {
        "id": cap_id,
        "title": f"🤖 {slot.get('summary') or '自动沉淀的脚本'}",
        "description": (f"自动沉淀：这段脚本在 {slot['host']} 上成功运行了 {slot['ok_runs']} 次，"
                        f"由 web-bridge 从 exec 日志里自动存成能力（首次 {slot['first']}）。"
                        f"标题和参数说明是机器生成的，确认有用就用 save_capability 覆盖同名文件、写好描述。"),
        "kind": "other",
        "match": [slot["host"]] if slot["host"] and slot["host"] != "?" else ["*"],
        "params": _params_from_args(slot.get("args")),
        "auto": True,
        "runs": slot["ok_runs"],
        "first_seen": slot["first"],
    }
    source = "/* @web-bridge-capability\n" + json.dumps(meta, ensure_ascii=False, indent=2) + "\n*/\n" + code
    AUTO_DIR.mkdir(parents=True, exist_ok=True)
    path = AUTO_DIR / f"{cap_id}.js"
    path.write_text(source, encoding="utf-8")
    if not capabilities.get(cap_id):        # header must parse, or it isn't a capability
        path.unlink(missing_ok=True)
        return None
    return cap_id


# --------------------------------------------------------------------------- #
# lookup — what has been done on this site before?
# --------------------------------------------------------------------------- #
def search(query: str = "", host: str = "", limit: int = 10,
           only_ok: bool = True) -> list[dict]:
    """Scripts previously run here, most-repeated first. This is the 'look before
    you write' path: an agent asks what has worked on this site instead of
    inventing a selector from scratch."""
    idx = _load_index()
    q = (query or "").lower()
    rows = []
    for key, slot in idx.items():
        if host and host.lower() not in slot.get("host", "").lower():
            continue
        if only_ok and not slot.get("ok_runs"):
            continue
        hay = f"{slot.get('summary','')} {slot.get('code','')} {slot.get('host','')} {slot.get('capability') or ''}".lower()
        if q and q not in hay:
            continue
        rows.append({
            "host": slot.get("host"), "signature": slot.get("sig"),
            "kind": slot.get("kind"), "capability": slot.get("capability"),
            "promoted_to": slot.get("promoted_to"),
            "summary": slot.get("summary"), "runs": slot.get("runs"),
            "ok_runs": slot.get("ok_runs"), "first": slot.get("first"), "last": slot.get("last"),
            "code": slot.get("code"), "args": slot.get("args"),
        })
    rows.sort(key=lambda r: (r["ok_runs"] or 0, r["last"] or ""), reverse=True)
    return rows[:limit]


def record_discovery(url: str, offered: int, site_specific: int) -> None:
    """Note that someone asked what a page can do.

    Runs were journalled but discovery was not, so "the agent wrote JS instead
    of using the tool" could not be told apart from "the agent never looked" —
    and those need opposite fixes.
    """
    entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": "discover",
             "host": host_of(url), "url": (url or "")[:300], "ok": True,
             "offered": offered, "site_specific": site_specific}
    _append(entry)


def usage_stats(days: int = 7, host: str = "") -> dict:
    """Are the saved tools actually being used, or is everything hand-written?

    The library only pays off if it gets called. Until now the only way to know
    was to read the JSONL by hand — so the question "did it use my Agent Tool?"
    had no answer a user could get on their own.
    """
    import datetime
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = []
    try:
        with LOG_PATH.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if r.get("t", "") < cutoff:
                    continue
                if host and host.lower() not in (r.get("host") or "").lower():
                    continue
                rows.append(r)
    except OSError:
        pass

    by_kind: dict[str, int] = {}
    tools: dict[str, dict] = {}
    hosts: dict[str, dict] = {}
    for r in rows:
        kind = r.get("kind") or "exec"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        h = hosts.setdefault(r.get("host") or "?", {"capability": 0, "exec": 0, "user-script": 0})
        h[kind] = h.get(kind, 0) + 1
        if kind == "capability" and r.get("capability"):
            t = tools.setdefault(r["capability"], {"runs": 0, "ok": 0, "ms": 0})
            t["runs"] += 1
            t["ok"] += 1 if r.get("ok") else 0
            t["ms"] += int(r.get("ms") or 0)

    cap = by_kind.get("capability", 0)
    adhoc = by_kind.get("exec", 0)
    total = cap + adhoc
    # Sites where JS keeps getting written and no capability exists are a supply
    # problem (nothing to hit); sites with capabilities that still get ad-hoc JS
    # are a targeting problem. They need opposite fixes, so name them apart.
    import capabilities as _caps
    gaps = []
    for h in sorted(hosts, key=lambda k: hosts[k].get("exec", 0), reverse=True):
        if hosts[h].get("exec", 0) < 5 or h == "?":
            continue
        has_site_tool = any(
            c.get("match") != ["*"] and any(m.strip("*.") in h for m in (c.get("match") or []))
            for c in _caps.all_caps())
        gaps.append({"host": h, "adhoc": hosts[h]["exec"],
                     "capability_runs": hosts[h].get("capability", 0),
                     "has_site_tool": has_site_tool})
    return {
        "days": days,
        "capability_runs": cap,
        "adhoc_execs": adhoc,
        "discoveries": by_kind.get("discover", 0),
        # sites doing ad-hoc work: with no tool = nothing to hit, with a tool =
        # the tool is not being reached for
        "gaps": gaps[:6],
        "user_script_runs": by_kind.get("user-script", 0),
        # the number that answers "is the library earning its keep"
        "reuse_rate": round(cap / total, 3) if total else 0.0,
        "tools": sorted(
            ({"id": k, **v, "avg_ms": round(v["ms"] / v["runs"]) if v["runs"] else 0}
             for k, v in tools.items()),
            key=lambda t: t["runs"], reverse=True),
        "hosts": sorted(
            ({"host": k, **v} for k, v in hosts.items()),
            key=lambda h: h["capability"] + h["exec"] + h["user-script"], reverse=True)[:10],
    }


def stats() -> dict:
    idx = _load_index()
    return {
        "log": str(LOG_PATH),
        "index": str(INDEX_PATH),
        "distinct_scripts": sum(1 for s in idx.values() if s.get("kind") == "exec"),
        "capability_runs": sum(s.get("runs", 0) for s in idx.values() if s.get("kind") == "capability"),
        "exec_runs": sum(s.get("runs", 0) for s in idx.values() if s.get("kind") == "exec"),
        "promoted": [s["promoted_to"] for s in idx.values() if s.get("promoted_to")],
        "promote_after": PROMOTE_AFTER,
    }

"""Exec journal — the memory that turns one-off scripts into capabilities.

Every `/exec` and `/capability/{id}` call is appended to a greppable JSONL log.
Identical scripts are recognised by a normalised hash, counted per host, and once
This is a record of what happened, not a judgement about it. Deciding that a
script is worth keeping — and what to call it, and which of its literals are
really parameters — is the agent's job; every hardcoded attempt at it here got
it wrong. After a script succeeds a couple of times the agent is reminded that
it might be worth saving, and that is as far as this goes.

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
# How many successes before reminding the agent that this looks reusable.
# A reminder, not a threshold: nothing happens on its own at any count.
REMIND_AFTER = int(config.CFG.get("remind_after", 2))


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
    # No auto-promotion. The gateway records what happened; deciding that a
    # script is worth keeping, what to call it, and which of its literals are
    # really parameters are judgements, and every hardcoded attempt at them here
    # got it wrong: the triviality filter rejected innerText, the repeat counter
    # picked the invariant `click send` half of a task over the half that
    # carried the data, and per-value signatures hid a task repeating four
    # times. The agent is asked to save its own work instead — it knows which
    # script was the answer, because it just used the answer.
    if kind == "exec" and ok and slot["ok_runs"] >= REMIND_AFTER:
        out["hint"] = (f"这段脚本在 {host} 上成功跑了 {slot['ok_runs']} 次了。"
                       f"如果它值得复用，用 web_save_capability 存成能力："
                       f"起个说人话的名字、写清楚它做什么、把会变的部分声明成参数。"
                       f"你比这里的任何规则都清楚它该不该存、参数是哪几个。")
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


# Scripts that repeat but are not worth keeping. A capability is something an
# agent should reach for later; `location.reload()` run twenty times during
# debugging is not, and one of them did end up in the library titled
# "🤖 location.reload();return 1".
TRIVIAL_PATTERNS = (
    "location.reload", "location.href=", "window.close", "document.title",
    "return 1", "return true", "return null", "console.log",
)






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


def record_journal_read(query: str, host: str, matches: int) -> None:
    """Note that someone searched the journal before writing JS.

    Same reason `discover` is recorded: without it, "the agent looked at what
    ran here before and wrote its own anyway" and "the agent never looked" are
    indistinguishable, and they need opposite fixes. The skill tells agents to
    check here first; this is how we find out whether they do.
    """
    entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": "journal-read",
             "host": host or "?", "url": "", "ok": True,
             "query": (query or "")[:120], "matches": matches}
    _append(entry)


def recent(limit: int = 25, host: str = "") -> list[dict]:
    """The last N things that happened, newest first — a feed, not an aggregate.

    search() groups by signature and sorts by repeat count, which answers "what
    works here" but cannot answer "did it just use my tool or write its own
    again". Watching reuse happen needs a timeline.
    """
    rows: list[dict] = []
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
                if host and host.lower() not in (r.get("host") or "").lower():
                    continue
                rows.append({
                    "t": r.get("t"), "kind": r.get("kind"), "host": r.get("host"),
                    "capability": r.get("capability"), "ok": r.get("ok"),
                    "ms": r.get("ms"), "sig": r.get("sig"),
                    # enough to recognise a script, never the whole body
                    "summary": (r.get("summary") or "")[:70],
                    "matches": r.get("matches"), "offered": r.get("offered"),
                })
    except OSError:
        return []
    return rows[-limit:][::-1]


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
    # Reuse turned out not to look like "the same script ran again". Measured
    # 2026-08-27: an agent that read the journal first wrote 4 scripts and got it
    # right first try, where two agents that did not wrote 16 and 10 on the same
    # kind of task -- and none of the three shared a single signature. What it
    # took from the journal was the endpoint and the payload shape, then it wrote
    # its own code. So the number that matters is not signature repeats but
    # whether prior work was read at all, and how many scripts a session then
    # needed. Counting repeats alone reports this as zero reuse.
    reads = by_kind.get("journal-read", 0)
    discoveries = by_kind.get("discover", 0)
    looked = reads + discoveries
    # Sites where JS keeps getting written and no capability exists are a supply
    # problem (nothing to hit); sites with capabilities that still get ad-hoc JS
    # are a targeting problem. They need opposite fixes, so name them apart.
    # Raw exec count cannot tell a capability gap from ordinary development.
    # Writing a page script is dozens of one-off probes — 37 scripts, 37 distinct
    # signatures, each run once — and reading that as "this site needs a tool"
    # sends the agent nagging about work that was never repeated. What marks a
    # real gap is REPETITION: the same work done again and again.
    import capabilities as _caps
    per_host_sigs: dict[str, dict[str, int]] = {}
    per_host_days: dict[str, set] = {}
    for r in rows:
        if r.get("kind") != "exec":
            continue
        h = r.get("host") or "?"
        sig = r.get("sig") or ""
        per_host_sigs.setdefault(h, {})
        per_host_sigs[h][sig] = per_host_sigs[h].get(sig, 0) + 1
        per_host_days.setdefault(h, set()).add((r.get("t") or "")[:10])

    gaps = []
    for h in sorted(hosts, key=lambda k: hosts[k].get("exec", 0), reverse=True):
        runs = hosts[h].get("exec", 0)
        if runs < 5 or h == "?":
            continue
        sigs = per_host_sigs.get(h, {})
        distinct = len(sigs)
        repeats = sum(v for v in sigs.values() if v > 1)
        has_site_tool = any(
            c.get("match") != ["*"] and any(m.strip("*.") in h for m in (c.get("match") or []))
            for c in _caps.all_caps())
        # every script different, each run once = someone was building something
        # here, not repeating a task a tool could have done
        authoring = distinct >= max(5, runs * 0.8) and repeats <= 1
        gaps.append({"host": h, "adhoc": runs, "distinct": distinct,
                     "repeats": repeats, "days": len(per_host_days.get(h, ())),
                     "capability_runs": hosts[h].get("capability", 0),
                     "has_site_tool": has_site_tool,
                     "looks_like_authoring": authoring})
    return {
        "days": days,
        "capability_runs": cap,
        "adhoc_execs": adhoc,
        "discoveries": discoveries,
        "journal_reads": reads,
        # Reuse of knowledge, not of code: whether a session consulted prior work
        # before writing. The measured effect is on how many scripts it then
        # needs, not on whether any of them repeat.
        "consulted_prior": looked,
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
        # `promoted_to` is legacy: nothing writes it any more, but old entries
        # still carry it and it is still true of them.
        "promoted": [s["promoted_to"] for s in idx.values() if s.get("promoted_to")],
        "remind_after": REMIND_AFTER,
    }

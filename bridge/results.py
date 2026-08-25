"""Result store — a finished answer survives the connection that asked for it.

The failure this exists for: `wb chatgpt --images` drove ChatGPT for four
minutes, the page produced the pictures, and then the HTTP connection died
(server restarted, client gave up, laptop slept). The work was done and paid for
in real account quota, but the only copy of the answer was in flight on a socket
that no longer existed, so it was gone.

So every command may carry a caller-generated `request_id`:

  * the id is registered as *running* before the command is sent to the browser;
  * a second request with the same id does NOT re-run anything — it attaches to
    the run already in progress (a retry must never re-send a prompt to ChatGPT
    and charge the user twice);
  * the outcome (success or failure) is kept in memory AND written to
    `<state>/results/<id>.json`, so it can be claimed later with
    `GET /result/{id}` — even by a different process, even after a restart.

Deliberately NOT a job queue: entries expire (TTL), the store is capped, and it
holds nothing the caller did not already ask for.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import config

STATE_DIR = Path(os.environ.get("WEB_BRIDGE_STATE", str(config.CONFIG_PATH.parent)))
RESULT_DIR = STATE_DIR / "results"

TTL_SECONDS = float(config.CFG.get("result_ttl_seconds", 3600))
# One ChatGPT image comes back as ~1-3 MB of base64; three of them plus text is
# still small next to this ceiling, which exists only to stop a runaway page
# from filling the disk.
MAX_BYTES = int(config.CFG.get("result_max_bytes", 96 * 1024 * 1024))
MAX_ENTRIES = int(config.CFG.get("result_max_entries", 200))

_mem: dict[str, dict] = {}


# --------------------------------------------------------------------------- #
# disk
# --------------------------------------------------------------------------- #
def _path(request_id: str) -> Path:
    safe = "".join(c for c in request_id if c.isalnum() or c in "-_")[:80]
    return RESULT_DIR / f"{safe}.json"


def _persist(entry: dict) -> None:
    """Write a finished entry to disk. Best effort: losing the disk copy only
    costs the ability to claim it after a restart, never the live response."""
    try:
        RESULT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            RESULT_DIR.chmod(0o700)
        except OSError:
            pass
        blob = json.dumps(entry, ensure_ascii=False)
        if len(blob.encode()) > MAX_BYTES:
            small = dict(entry)
            small["result"] = None
            small["truncated"] = True
            small["error"] = (small.get("error") or
                              f"结果太大（>{MAX_BYTES // 1024 // 1024}MB），未落盘；内存里仍可领取")
            blob = json.dumps(small, ensure_ascii=False)
        p = _path(entry["request_id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(blob, encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        pass


def _load(request_id: str) -> Optional[dict]:
    try:
        p = _path(request_id)
        if not p.is_file():
            return None
        entry = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - entry.get("finished", 0) > TTL_SECONDS:
            p.unlink(missing_ok=True)
            return None
        return entry
    except Exception:  # noqa: BLE001
        return None


def sweep() -> None:
    """Drop expired entries from memory and disk."""
    now = time.time()
    for rid, e in list(_mem.items()):
        done_at = e.get("finished")
        if done_at and now - done_at > TTL_SECONDS:
            _mem.pop(rid, None)
        elif not done_at and now - e.get("started", now) > TTL_SECONDS * 2:
            _mem.pop(rid, None)          # a run whose process died mid-flight
    if len(_mem) > MAX_ENTRIES:
        for rid, _ in sorted(_mem.items(), key=lambda kv: kv[1].get("started", 0))[: len(_mem) - MAX_ENTRIES]:
            _mem.pop(rid, None)
    try:
        if RESULT_DIR.is_dir():
            for p in RESULT_DIR.glob("*.json"):
                if now - p.stat().st_mtime > TTL_SECONDS:
                    p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def begin(request_id: str, meta: dict) -> Optional[dict]:
    """Claim an id for a new run.

    Returns None when this call owns the run. Returns the existing entry when
    the id is already known — the caller must then attach to it (or return its
    stored outcome) instead of running the command a second time.
    """
    sweep()
    existing = _mem.get(request_id) or _load(request_id)
    if existing is not None:
        _mem.setdefault(request_id, existing)
        return existing
    _mem[request_id] = {
        "request_id": request_id,
        "status": "running",
        "started": time.time(),
        "finished": None,
        "meta": meta,
        "result": None,
        "error": None,
        "code": None,
    }
    return None


def done(request_id: str, result: Any) -> None:
    e = _mem.get(request_id)
    if e is None:
        e = {"request_id": request_id, "started": time.time(), "meta": {}}
        _mem[request_id] = e
    e.update(status="done", finished=time.time(), result=result, error=None, code=None)
    _persist(e)


def fail(request_id: str, code: int, error: str) -> None:
    e = _mem.get(request_id)
    if e is None:
        e = {"request_id": request_id, "started": time.time(), "meta": {}}
        _mem[request_id] = e
    e.update(status="error", finished=time.time(), result=None, error=error, code=code)
    _persist(e)


def get(request_id: str) -> Optional[dict]:
    e = _mem.get(request_id)
    if e is None:
        e = _load(request_id)
        if e is not None:
            _mem[request_id] = e
    return e


async def wait(request_id: str, timeout: float) -> Optional[dict]:
    """Block until the run behind this id finishes (or the wait runs out).

    Polling rather than an asyncio.Event on purpose: an Event built in one loop
    and awaited in another is exactly the bug that used to take this server
    down, and a 0.25s poll costs nothing next to a multi-minute page command.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        e = get(request_id)
        if e is None or e.get("status") != "running":
            return e
        if time.monotonic() >= deadline:
            return e
        await asyncio.sleep(0.25)


def public(entry: dict, with_result: bool = True) -> dict:
    out = {
        "request_id": entry.get("request_id"),
        "status": entry.get("status"),
        "started": entry.get("started"),
        "finished": entry.get("finished"),
        "age_seconds": round(time.time() - (entry.get("started") or time.time()), 1),
        "meta": entry.get("meta") or {},
    }
    if entry.get("error"):
        out["error"] = entry["error"]
        out["code"] = entry.get("code")
    if entry.get("truncated"):
        out["truncated"] = True
    if with_result and entry.get("status") == "done":
        out["result"] = entry.get("result")
    return out


def recent(limit: int = 20) -> list[dict]:
    sweep()
    known = dict(_mem)
    try:
        for p in RESULT_DIR.glob("*.json") if RESULT_DIR.is_dir() else []:
            rid = p.stem
            if rid not in known:
                e = _load(rid)
                if e:
                    known[rid] = e
    except Exception:  # noqa: BLE001
        pass
    rows = sorted(known.values(), key=lambda e: e.get("started") or 0, reverse=True)[:limit]
    return [public(e, with_result=False) for e in rows]

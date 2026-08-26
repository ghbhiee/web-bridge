#!/usr/bin/env python3
"""web-bridge server — authenticated local relay.

Lets any local client (CLI / MCP / another agent) drive a logged-in browser page
via the web-bridge extension:

    client --HTTP(+token)--> this server --WebSocket(+token)--> extension SW
                                                                     |
                                                    runs JS in a tab's MAIN world
                                                    (exec) or a site adapter method

Security:
  * binds 127.0.0.1 only (config.host)
  * every HTTP route requires `Authorization: Bearer <token>`
  * the extension WS must present the same token (?token= or first hello)
  * commands aimed at the same site/url are serialized; different targets run
    concurrently, and a caller that would have to queue is told what is running

The generic primitive is POST /exec: run a JS function body in a page's MAIN
world and get its (JSON-serializable) return value. Everything else — site
adapters, chatgpt.ask — is layered on top of exec inside the extension.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
import uvicorn

import config
import capabilities
import journal
import agents
import user_scripts
import results

VERSION = "0.2.0"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Make an interrupted run legible in the log.

    A restart in the middle of a four-minute page command reaches the caller as
    `RemoteDisconnected` and nothing else — no traceback, because nothing
    crashed: something outside sent a signal (`launchctl kickstart -k`, i.e.
    `wb service restart`, is the usual culprit). Saying so on the way out turns
    a silent hole in the log into one line that names the victims.
    """
    yield
    if hub.inflight:
        for key, v in hub.inflight.items():
            print(f"[web-bridge] ⚠️  服务退出时仍有命令在跑：目标 {key} "
                  f"{v.get('action')}/{v.get('method')} 已 {time.time() - v['started']:.0f}s"
                  f"（调用方会收到 RemoteDisconnected；带 request_id 的调用可用 /result/{{id}} 补捞）",
                  flush=True)
    else:
        print("[web-bridge] 收到退出信号，没有在跑的命令", flush=True)


app = FastAPI(title="web-bridge", version=VERSION, lifespan=lifespan)

STARTED_AT = time.time()
HERE = Path(__file__).resolve().parent


def build_info() -> dict:
    """Which code is this process actually running?

    "The fix is on disk but the daemon is still the old process" cost a whole
    debugging session once: the live traceback pointed at a line number that no
    longer existed, so the crash looked unfixed. /health now says when the
    process started, what the sources hash to, and whether anything on disk has
    been edited since — `stale: true` means restart before believing anything.
    """
    # Only the modules this process actually imports: editing cli.py does not
    # make the daemon stale, and a warning that cries wolf gets ignored.
    files = [HERE / n for n in ("server.py", "config.py", "capabilities.py",
                                "journal.py", "results.py")]
    h = hashlib.sha256()
    newest = 0.0
    newest_file = ""
    for p in files:
        try:
            st = p.stat()
            h.update(p.name.encode())
            h.update(p.read_bytes())
            if st.st_mtime > newest:
                newest, newest_file = st.st_mtime, p.name
        except OSError:
            continue
    return {
        "version": VERSION,
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STARTED_AT)),
        "started_epoch": round(STARTED_AT, 3),
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "code_sha256": h.hexdigest()[:12],
        "code_mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest)) if newest else None,
        "code_newest_file": newest_file,
        "stale": bool(newest and newest > STARTED_AT),
    }


BUILD_AT_START = build_info()


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def require_token(authorization: str = Header(default="")) -> None:
    if not config.TOKEN:
        # no token configured → refuse rather than run open
        raise HTTPException(status_code=500, detail="服务未配置 token（~/.config/web-bridge/config.json）")
    expected = f"Bearer {config.TOKEN}"
    # constant-time-ish compare
    if len(authorization) != len(expected) or not _consteq(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def _consteq(a: str, b: str) -> bool:
    res = 0
    for x, y in zip(a, b):
        res |= ord(x) ^ ord(y)
    return res == 0


# --------------------------------------------------------------------------- #
# extension connection hub
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self) -> None:
        self.ws: Optional[WebSocket] = None
        self.connected_at = 0.0
        self.last_seen = 0.0
        self.info: dict[str, Any] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_loop = None
        self.inflight: dict[str, dict] = {}

    def lock(self, key: str) -> asyncio.Lock:
        """One lock per target, created inside the loop that will await it.

        Two bugs live here, both found the hard way:

        1. Built at import time (Python 3.9), `asyncio.Lock()` binds to whatever
           `get_event_loop()` returned then — a loop uvicorn never runs. The
           uncontended fast path never touches that binding, so it worked for
           months; the first time two requests overlapped, the second died with
           "got Future attached to a different loop" and the caller got a 500.

        2. It used to be ONE lock for everything. A `chatgpt.ask` holds its lock
           for minutes, so every unrelated call — even on another site — queued
           behind it and eventually timed out with no explanation. Worse, a
           client that gave up left the command holding the lock until its own
           305s timeout expired. Keying by target keeps unrelated work moving.
        """
        loop = asyncio.get_running_loop()
        if self._lock_loop is not loop:
            self._locks = {}
            self._lock_loop = loop
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def connected(self) -> bool:
        return self.ws is not None

    async def wait_connected(self, timeout: float = 6.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ws is not None:
                return True
            await asyncio.sleep(0.15)
        return self.ws is not None

    async def command(self, action: str, payload: dict, timeout: float,
                      serialize: bool = True, queue_wait: float = 20.0) -> dict:
        if not await self.wait_connected():
            raise HTTPException(status_code=503, detail="扩展未连接（在 chrome://extensions 加载/重载 web-bridge 扩展）")
        # Commands that only read/arrange tabs never inject anything, so they
        # have no reason to wait behind a page-driving command.
        if not serialize:
            return await self._send(action, payload, timeout)
        key = payload.get("site") or payload.get("url") or "*active-tab*"
        lock = self.lock(key)
        if lock.locked():
            busy = self.inflight.get(key) or {}
            age = time.time() - busy.get("started", time.time())
            try:
                await asyncio.wait_for(lock.acquire(), timeout=queue_wait)
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=503,
                    detail=f"目标 '{key}' 上已有命令在跑（{busy.get('action', '?')}"
                           f"{'/' + busy['method'] if busy.get('method') else ''}，已 {age:.0f}s），"
                           f"等了 {queue_wait:.0f}s 仍未轮到。稍后重试，或换一个 site/url 目标——"
                           f"不同目标之间不互相排队。")
        else:
            await lock.acquire()
        self.inflight[key] = {"action": action, "method": payload.get("method"),
                              "started": time.time()}
        try:
            return await self._send(action, payload, timeout)
        finally:
            self.inflight.pop(key, None)
            lock.release()

    def notify(self, action: str, payload: Optional[dict] = None) -> None:
        """Tell the extension something changed, without waiting for a reply.

        Used after a capability edit so autorun registration refreshes right
        away. Deliberately not a command: nobody is waiting on a result, and it
        must never queue behind a page-driving command.
        """
        ws = self.ws
        if ws is None:
            return
        msg = json.dumps({"type": "notify", "action": action, "payload": payload or {}})
        try:
            asyncio.get_running_loop().create_task(ws.send_text(msg))
        except Exception:  # noqa: BLE001
            pass

    async def _send(self, action: str, payload: dict, timeout: float) -> dict:
        """Ship one command to the extension and wait for its result by id."""
        ws = self.ws
        if ws is None:
            raise HTTPException(status_code=503, detail="扩展连接已断开")
        rid = uuid.uuid4().hex
        if config.CFG.get("debug_ws"):
            print(f"[web-bridge] ws-> command {action} id={rid[:8]}", flush=True)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending[rid] = fut
        try:
            await ws.send_text(json.dumps({"type": "command", "id": rid, "action": action, "payload": payload}))
        except Exception as e:  # noqa: BLE001
            self.pending.pop(rid, None)
            raise HTTPException(status_code=503, detail=f"无法发送到扩展：{e}")
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=f"等待扩展响应超时（{timeout:.0f}s）")
        finally:
            self.pending.pop(rid, None)
        if not result.get("ok"):
            raise HTTPException(status_code=502, detail=result.get("error") or "扩展执行失败")
        return result.get("data") or {}


hub = Hub()

DEFAULT_PORT = 8790


def _mock_allowed() -> bool:
    """Test doubles are welcome on a throwaway instance, never on the live one."""
    return config.PORT != DEFAULT_PORT or os.environ.get("WEB_BRIDGE_ALLOW_MOCK") == "1"


@app.websocket("/ws/ext")
async def ws_ext(ws: WebSocket):
    token = ws.query_params.get("token", "")
    if not config.TOKEN or token != config.TOKEN:
        await ws.close(code=4401)
        return
    # Only the extension may hold this socket. A browser page that somehow
    # learned the token would connect with its own http(s) Origin; the
    # extension's service worker connects with a chrome-extension:// Origin (or
    # none). Reject web origins so a malicious page can't impersonate it.
    origin = ws.headers.get("origin", "")
    if origin and not origin.startswith("chrome-extension://"):
        print(f"[web-bridge] rejected ws from origin {origin}")
        await ws.close(code=4403)
        return
    # The hub has exactly one extension slot, so a mock client is not a harmless
    # extra connection — it *evicts* the real extension, which reconnects and
    # evicts the mock, and the two ping-pong until the test ends. That really
    # happened (10 takeovers/second for 10 seconds against the live service) and
    # it reads in the log as "the extension keeps disconnecting". A mock must
    # therefore say so, and is refused on the production port.
    if ws.query_params.get("client") == "mock" and not _mock_allowed():
        print(f"[web-bridge] 拒绝 mock 客户端连接生产服务（端口 {config.PORT}）——"
              f"跑测试请用 bridge/run_tests.sh（独立端口 + 临时 state）")
        await ws.close(code=4403)
        return
    await ws.accept()
    # Only one extension socket is live at a time. If something else already
    # holds the slot (a stale SW instance, a second browser profile, a test),
    # close it explicitly so its side notices and reconnects — otherwise it sits
    # there believing it is connected while the hub talks to someone else.
    old = hub.ws
    hub.ws = ws
    if old is not None and old is not ws:
        try:
            await old.close(code=4409)
        except Exception:  # noqa: BLE001
            pass
    hub.connected_at = hub.last_seen = time.time()
    # Name the takeover: repeated "connected/disconnected" pairs used to look
    # like a flaky extension when they were actually two clients fighting over
    # the one slot. Now the log says which, and whether a slot change happened.
    who = ws.query_params.get("client") or "extension"
    print(f"[web-bridge] {who} connected {time.strftime('%H:%M:%S')}"
          + (f" (顶掉了上一个 {hub.info.get('client') or '连接'})" if old is not None and old is not ws else ""))
    try:
        while True:
            raw = await ws.receive_text()
            hub.last_seen = time.time()
            # Re-adopt an orphaned-but-live socket: if another connection took
            # the slot and then went away, hub.ws is None while this extension is
            # still here and talking. Its next ping (≤20s) heals the link instead
            # of leaving `wb status` reporting a disconnection that isn't real.
            if hub.ws is None:
                hub.ws = ws
                print("[web-bridge] re-adopted live extension socket")
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")
            if config.CFG.get("debug_ws"):
                print(f"[web-bridge] ws<- {t} {str(msg)[:160]}", flush=True)
            if t == "hello":
                hub.info = msg.get("info") or {}
            elif t == "ping":
                await ws.send_text(json.dumps({"type": "pong", "t": time.time()}))
            elif t == "result":
                fut = hub.pending.get(msg.get("id"))
                if fut and not fut.done():
                    fut.set_result(msg)
            elif t in ("progress", "pong"):
                pass
    except WebSocketDisconnect:
        print("[web-bridge] extension disconnected")
    except Exception as e:  # noqa: BLE001
        print(f"[web-bridge] ws error: {e}")
    finally:
        if hub.ws is ws:
            hub.ws = None
        # Do NOT fail in-flight futures: a recycled MV3 SW reconnects and the
        # result arrives on the new socket. Per-request timeout catches dead ones.


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health():
    # health is unauthenticated but reveals nothing sensitive
    build = build_info()
    return {
        "ok": True,
        "extension_connected": hub.connected(),
        "seconds_since_seen": (time.time() - hub.last_seen) if hub.last_seen else None,
        "info": hub.info,
        "sites": list(config.SITES.keys()),
        "inflight": [{"target": k, **v, "seconds": round(time.time() - v["started"], 1)}
                     for k, v in hub.inflight.items()],
        "live_agent_runs": agents.live_runs(),
        "version": BUILD_AT_START.get("version"),
        "build": build,
        # spelled out so `curl /health | grep stale` is enough to catch the
        # "daemon is older than the fix" trap without reading the whole object
        "stale_code": build["stale"],
    }


# --------------------------------------------------------------------------- #
# result cache — a finished answer outlives the connection that asked for it
# --------------------------------------------------------------------------- #
async def with_result_cache(request_id: Optional[str], meta: dict,
                            run: Callable[[], Awaitable[dict]]) -> dict:
    """Run `run()` once per request_id, remembering the outcome.

    Without an id this is a plain call (old behaviour). With one:
      * the outcome is stored, so `GET /result/{id}` can hand it over after the
        original connection died;
      * a repeat of the same id attaches to the run in progress instead of
        starting a second one — a client retry must never re-send a prompt and
        bill the user's ChatGPT quota twice.
    """
    if not request_id:
        return await run()
    existing = results.begin(request_id, meta)
    if existing is not None:
        if existing.get("status") == "running":
            # someone is already doing this exact work — wait for their answer
            done = await results.wait(request_id, timeout=meta.get("timeout", 300) + 15)
            if done and done.get("status") == "done":
                return done.get("result") or {}
            if done and done.get("status") == "error":
                raise HTTPException(status_code=done.get("code") or 502, detail=done.get("error"))
            raise HTTPException(status_code=504,
                                detail=f"request_id {request_id} 上的命令仍在跑，等待超时；"
                                       f"用 GET /result/{request_id}?wait=N 继续等")
        if existing.get("status") == "done":
            return existing.get("result") or {}
        raise HTTPException(status_code=existing.get("code") or 502, detail=existing.get("error"))
    try:
        data = await run()
    except HTTPException as e:
        results.fail(request_id, e.status_code, str(e.detail))
        raise
    except Exception as e:  # noqa: BLE001
        results.fail(request_id, 500, str(e))
        raise
    results.done(request_id, data)
    return data


@app.get("/result/{request_id}", dependencies=[Depends(require_token)])
async def get_result(request_id: str, wait: float = 0.0):
    """Claim the outcome of an earlier request by its id.

    This is the answer to "the connection dropped and the work is gone": the
    page already did the job, the bridge already has the answer, and the client
    only lost the socket. `?wait=N` long-polls while the run is still going.
    """
    entry = results.get(request_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"没有 request_id '{request_id}' 的记录（可能已过期，或那次请求在服务重启前就没跑完）。"
                   f"ChatGPT 的答案可以用 `wb chatgpt-last` 直接从页面补捞。")
    if entry.get("status") == "running" and wait > 0:
        entry = await results.wait(request_id, timeout=min(wait, 600)) or entry
    return {"ok": True, **results.public(entry)}


@app.get("/results", dependencies=[Depends(require_token)])
async def list_results(limit: int = 20):
    """Recent request ids and their state (no payloads) — for `wb results`."""
    return {"ok": True, "results": results.recent(limit)}


class ExecReq(BaseModel):
    code: str                              # function body; `args` is in scope, may `return`
    args: Any = None
    site: Optional[str] = None             # named site from config (picks a matching tab)
    url: Optional[str] = None              # or an explicit url to match/open
    new_tab: bool = False
    activate: bool = True
    timeout_ms: int = 30000
    queue_wait_ms: int = 20000             # how long to queue behind a command
                                           # already running on the same target
    request_id: Optional[str] = None       # caller-generated; makes the result
                                           # claimable later via GET /result/{id}


def _guard_url(url: Optional[str], site: Optional[str] = None) -> None:
    """Refuse to inject into sensitive pages (banking, password managers, …).
    Checked on the bridge rather than in the extension so every caller — CLI,
    MCP, another agent — is covered by the same rule."""
    target = url or ""
    if not target and site:
        target = (config.SITES.get(site) or {}).get("home", "")
    hit = config.is_blocked(target)
    if hit:
        raise HTTPException(
            status_code=403,
            detail=f"拒绝在敏感页面上执行（匹配黑名单规则 '{hit}'）。"
                   f"如确需放行，编辑 ~/.config/web-bridge/config.json 的 blocklist。")


def _site_fields(site: Optional[str]) -> dict:
    """Resolve a named site to the match patterns / home / adapter the SW needs."""
    if not site:
        return {}
    s = config.SITES.get(site)
    if not s:
        raise HTTPException(status_code=404, detail=f"未知站点 '{site}'（config.sites 未定义）")
    return {"matches": s.get("match", []), "home": s.get("home"), "adapter": s.get("adapter")}


@app.post("/exec", dependencies=[Depends(require_token)])
async def exec_js(req: ExecReq):
    async def run() -> dict:
        # inside the cached call on purpose: a rejection (blocklist, unknown
        # site) is an outcome too, and an id that was accepted must always have
        # *something* to hand back later
        _guard_url(req.url, req.site)
        payload = {**req.model_dump(), **_site_fields(req.site)}
        payload.pop("request_id", None)
        return await hub.command("exec", payload, timeout=req.timeout_ms / 1000 + 5,
                                 queue_wait=req.queue_wait_ms / 1000)

    started = time.monotonic()
    try:
        data = await with_result_cache(
            req.request_id,
            {"kind": "exec", "site": req.site, "url": req.url, "timeout": req.timeout_ms / 1000},
            run)
    except HTTPException as e:
        # a failed run is worth remembering too: the next agent gets to see that
        # this approach was already tried here, and how it broke
        journal.record(kind="exec", code=req.code, args=req.args, url=req.url or "",
                       site=req.site or "", ok=False, error=str(e.detail),
                       ms=int((time.monotonic() - started) * 1000))
        raise
    note = journal.record(kind="exec", code=req.code, args=req.args,
                          url=data.get("tab_url") or req.url or "",
                          site=req.site or "", ok=True, result=data.get("result"),
                          ms=int((time.monotonic() - started) * 1000))
    return JSONResponse({"ok": True, **data, "journal": note})


class AdapterReq(BaseModel):
    params: dict = {}
    new_tab: bool = False
    timeout_ms: int = 300000
    request_id: Optional[str] = None       # claimable later via GET /result/{id}


@app.post("/adapter/{site}/{method}", dependencies=[Depends(require_token)])
async def adapter(site: str, method: str, req: AdapterReq):
    async def run() -> dict:
        _guard_url(None, site)
        payload = {"site": site, "method": method, **req.model_dump(), **_site_fields(site)}
        payload.pop("request_id", None)
        return await hub.command("adapter", payload, timeout=req.timeout_ms / 1000 + 5)

    data = await with_result_cache(
        req.request_id,
        {"kind": "adapter", "site": site, "method": method, "timeout": req.timeout_ms / 1000},
        run)
    return JSONResponse({"ok": True, **data})


@app.get("/tabs", dependencies=[Depends(require_token)])
async def tabs(filter: str = ""):
    data = await hub.command("tabs", {"filter": filter}, timeout=15, serialize=False)
    return JSONResponse({"ok": True, **data})


class OpenReq(BaseModel):
    url: str
    activate: bool = True
    reuse: bool = True


@app.post("/open", dependencies=[Depends(require_token)])
async def open_tab(req: OpenReq):
    data = await hub.command("open", req.model_dump(), timeout=40, serialize=False)
    return JSONResponse({"ok": True, **data})


class CloseReq(BaseModel):
    url: Optional[str] = None      # substring of the tab URL
    tab_id: Optional[int] = None   # or an exact tab id from /tabs


@app.post("/close", dependencies=[Depends(require_token)])
async def close_tab(req: CloseReq):
    """Close tabs by id or URL substring, so automation can clean up after
    itself. Deliberately requires an explicit target — there is no 'close all'."""
    if not req.url and req.tab_id is None:
        raise HTTPException(status_code=400, detail="必须给 url（URL 片段）或 tab_id")
    data = await hub.command("close", req.model_dump(), timeout=20, serialize=False)
    return JSONResponse({"ok": True, **data})


# --------------------------------------------------------------------------- #
# capabilities — the discoverable script library
# --------------------------------------------------------------------------- #
@app.get("/capabilities", dependencies=[Depends(require_token)])
async def list_capabilities(url: str = "", site: str = ""):
    """What can be done on this page. Pass ?url= (or ?site=) to filter; omit for all."""
    if site and not url:
        s = config.SITES.get(site) or {}
        url = (s.get("home") or "")
    caps = capabilities.for_url(url) if (url or site) else capabilities.all_caps()
    out = {"ok": True, "url": url, "count": len(caps),
           "capabilities": [capabilities.public(c) for c in caps]}
    # Discovery is the moment to say "this has been done here before". Putting it
    # here rather than in prose means an agent that never read the skill still
    # finds the trodden path, without having to know the journal exists.
    prior = journal.search(host=journal.host_of(url), limit=5) if url else []
    if prior:
        out["prior_scripts"] = [
            {k: v for k, v in row.items() if k in
             ("summary", "runs", "ok_runs", "last", "promoted_to", "capability", "signature")}
            for row in prior]
        out["prior_hint"] = (f"这个站点以前跑过 {len(prior)} 段脚本/能力（按使用次数排序）。"
                             f"要看代码：GET /journal?host=…，CLI `wb log --host …`，"
                             f"MCP web_journal。别重复造轮子。")
    return out


@app.get("/capability/{cap_id}", dependencies=[Depends(require_token)])
async def get_capability(cap_id: str):
    """One capability in full — metadata, parameter help, and its source. An
    agent reads this before calling something unfamiliar, or before editing it."""
    cap = capabilities.get(cap_id)
    if not cap:
        raise HTTPException(status_code=404, detail=f"未知能力 '{cap_id}'")
    return {"ok": True, "capability": capabilities.public(cap),
            "params_help": capabilities.params_help(cap), "source": cap["body"]}


class RunCapReq(BaseModel):
    params: dict = {}
    site: Optional[str] = None
    url: Optional[str] = None
    new_tab: bool = False
    timeout_ms: int = 120000
    queue_wait_ms: int = 20000             # same fail-fast knob as /exec: how long
                                           # to queue behind another command on
                                           # this target before answering "busy"
    request_id: Optional[str] = None       # claimable later via GET /result/{id}


@app.post("/capability/{cap_id}", dependencies=[Depends(require_token)])
async def run_capability(cap_id: str, req: RunCapReq):
    cap = capabilities.get(cap_id)
    if not cap:
        known = ", ".join(c["id"] for c in capabilities.all_caps())
        raise HTTPException(status_code=404, detail=f"未知能力 '{cap_id}'。已有：{known}")
    _guard_url(req.url, req.site)
    # Validate + fill defaults here rather than in the page: a bad argument
    # should come back as an explanation, not as a capability that silently
    # returns nothing because `args.item` was undefined.
    try:
        args = capabilities.validate_params(cap, req.params)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    payload = {
        # the capability body IS the exec code; params arrive as `args`
        "code": cap["body"],
        "args": args,
        "site": req.site,
        "url": req.url,
        "new_tab": req.new_tab,
        "timeout_ms": req.timeout_ms,
        "capability": cap_id,
        **_site_fields(req.site),
    }
    started = time.monotonic()
    try:
        data = await with_result_cache(
            req.request_id,
            {"kind": "capability", "capability": cap_id, "site": req.site, "url": req.url,
             "timeout": req.timeout_ms / 1000},
            lambda: hub.command("exec", payload, timeout=req.timeout_ms / 1000 + 5,
                                queue_wait=req.queue_wait_ms / 1000))
    except HTTPException as e:
        journal.record(kind="capability", capability=cap_id, args=args, url=req.url or "",
                       site=req.site or "", ok=False, error=str(e.detail),
                       ms=int((time.monotonic() - started) * 1000))
        raise
    note = journal.record(kind="capability", capability=cap_id, args=args,
                          url=data.get("tab_url") or req.url or "",
                          site=req.site or "", ok=True, result=data.get("result"),
                          ms=int((time.monotonic() - started) * 1000))
    return JSONResponse({"ok": True, "capability": cap_id, **data, "journal": note})


@app.get("/capabilities/autorun", dependencies=[Depends(require_token)])
async def autorun_list():
    """What the extension should register to run on page load.

    Both libraries feed this: the agent's capabilities and the user's own
    scripts. They are separate everywhere else, but page-load registration is
    one mechanism.
    """
    return {"ok": True, "scripts": capabilities.autorun_for_registration()
                                   + user_scripts.autorun_for_registration()}


# --------------------------------------------------------------------------- #
# user scripts — the user's own page JS, kept apart from agent capabilities
# --------------------------------------------------------------------------- #
class AutorunReq(BaseModel):
    autorun: bool


class UserScriptReq(BaseModel):
    code: str
    by: Optional[str] = None               # which agent wrote it, for the list
    # None means "leave whatever is stored" — an update from the chat sends only
    # the code, and must not reset the switches the user set in the panel
    name: Optional[str] = None
    matches: Optional[list[str]] = None
    autorun: Optional[bool] = None
    note: Optional[str] = None


@app.get("/user-scripts", dependencies=[Depends(require_token)])
async def list_user_scripts(url: str = ""):
    return {"ok": True, "scripts": user_scripts.for_url(url),
            "total": len(user_scripts.all_scripts())}


@app.put("/user-script/{script_id}", dependencies=[Depends(require_token)])
async def put_user_script(script_id: str, req: UserScriptReq):
    try:
        rec = user_scripts.save({**req.model_dump(),
                                 "id": None if script_id == "new" else script_id})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    hub.notify("sync-autorun")
    return {"ok": True, "script": rec}


@app.delete("/user-script/{script_id}", dependencies=[Depends(require_token)])
async def delete_user_script(script_id: str):
    gone = user_scripts.delete(script_id)
    if gone:
        hub.notify("sync-autorun")
    return {"ok": gone}


@app.post("/user-script/{script_id}/autorun", dependencies=[Depends(require_token)])
async def user_script_autorun(script_id: str, req: AutorunReq):
    try:
        rec = user_scripts.set_autorun(script_id, req.autorun)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    hub.notify("sync-autorun")
    return {"ok": True, "script": rec}


class ImportReq(BaseModel):
    data: dict
    overwrite: bool = False


@app.get("/user-scripts/export", dependencies=[Depends(require_token)])
async def export_user_scripts(ids: str = ""):
    """Whole library, or `?ids=a,b`. Plain JSON so it can be diffed and edited."""
    wanted = [i for i in ids.split(",") if i] or None
    return user_scripts.export_bundle(wanted)


@app.post("/user-scripts/import", dependencies=[Depends(require_token)])
async def import_user_scripts(req: ImportReq):
    try:
        report = user_scripts.import_bundle(req.data, req.overwrite)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    hub.notify("sync-autorun")
    return {"ok": True, **report}


@app.get("/capabilities/export", dependencies=[Depends(require_token)])
async def export_capabilities(ids: str = ""):
    wanted = [i for i in ids.split(",") if i] or None
    return capabilities.export_bundle(wanted)


@app.post("/capabilities/import", dependencies=[Depends(require_token)])
async def import_capabilities(req: ImportReq):
    try:
        report = capabilities.import_bundle(req.data, req.overwrite)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    hub.notify("sync-autorun")
    return {"ok": True, **report}


@app.get("/user-script/{script_id}/bookmarklet", dependencies=[Depends(require_token)])
async def user_script_bookmarklet(script_id: str):
    """Export as a bookmarklet, so a script can travel to another machine.

    Returns both forms: the `javascript:` URL, and a page carrying it as a
    draggable link — dragging is the only way to install one in Chrome.
    """
    script = user_scripts.get(script_id)
    if not script:
        raise HTTPException(status_code=404, detail=f"没有这个脚本：{script_id}")
    return {"ok": True, "name": script["name"],
            "url": user_scripts.bookmarklet(script),
            "html": user_scripts.bookmarklet_page(script)}


class RunUserScriptReq(BaseModel):
    url: Optional[str] = None
    timeout_ms: int = 60000


@app.post("/user-script/{script_id}/run", dependencies=[Depends(require_token)])
async def run_user_script(script_id: str, req: RunUserScriptReq):
    """Run one on the page. Goes through the same exec path as everything else,
    so the blocklist and the journal cover it too."""
    script = user_scripts.get(script_id)
    if not script:
        raise HTTPException(status_code=404, detail=f"没有这个脚本：{script_id}")
    _guard_url(req.url, None)
    started = time.monotonic()
    payload = {"code": script["code"], "args": {}, "url": req.url,
               "timeout_ms": req.timeout_ms}
    try:
        data = await hub.command("exec", payload, timeout=req.timeout_ms / 1000 + 5)
    except HTTPException as e:
        journal.record(kind="user-script", code=script["code"], url=req.url or "",
                       ok=False, error=str(e.detail),
                       ms=int((time.monotonic() - started) * 1000))
        raise
    journal.record(kind="user-script", code=script["code"],
                   url=data.get("tab_url") or req.url or "", ok=True,
                   result=data.get("result"), ms=int((time.monotonic() - started) * 1000))
    return JSONResponse({"ok": True, "script": script["name"], **data})


@app.post("/capability/{cap_id}/autorun", dependencies=[Depends(require_token)])
async def set_autorun(cap_id: str, req: AutorunReq):
    """Flip a script's auto-run switch (page-beauty's enhance toggle)."""
    try:
        meta = capabilities.set_autorun(cap_id, req.autorun)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    hub.notify("sync-autorun")
    return {"ok": True, "capability": meta}


class SaveCapReq(BaseModel):
    source: str
    overwrite: bool = True


@app.put("/capability/{cap_id}", dependencies=[Depends(require_token)])
async def save_capability(cap_id: str, req: SaveCapReq):
    """Author a new capability (or update one) by writing its source file."""
    try:
        meta = capabilities.save(cap_id, req.source, req.overwrite)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    hub.notify("sync-autorun")
    return {"ok": True, "capability": meta}


@app.delete("/capability/{cap_id}", dependencies=[Depends(require_token)])
async def delete_capability(cap_id: str):
    gone = capabilities.delete(cap_id)
    if gone:
        hub.notify("sync-autorun")
    return {"ok": gone}



# --------------------------------------------------------------------------- #
# local agents — the side panel's chat tab drives claude / codex / dsh
# --------------------------------------------------------------------------- #
@app.get("/agents", dependencies=[Depends(require_token)])
async def list_agents():
    """Which agent CLIs this machine has, and how they will be invoked."""
    return {"ok": True, **agents.roster()}


class DetectReq(BaseModel):
    cwd: Optional[str] = None
    full_access: bool = True


@app.post("/agents/detect", dependencies=[Depends(require_token)])
async def detect_agents(req: DetectReq):
    """Re-probe PATH and rewrite the agents block in config.json."""
    block = agents.detect(req.cwd, req.full_access)
    agents.save(block)
    return {"ok": True, **agents.roster()}


class AskReq(BaseModel):
    prompt: str
    agent: Optional[str] = None
    cwd: Optional[str] = None
    session_id: Optional[str] = None       # continue a previous conversation
    page: Optional[dict] = None            # {url, title} the panel is looking at


@app.post("/agent/ask", dependencies=[Depends(require_token)])
async def agent_ask(req: AskReq):
    """Start an agent and stream its events back as NDJSON.

    Streaming rather than request/response because these runs take minutes; the
    run is also kept server-side, so a panel that reloads mid-answer can reattach
    with GET /agent/run/{id} instead of losing the work.
    """
    if not (req.prompt or "").strip():
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    try:
        run = await agents.start(req.agent or "", req.prompt,
                                 req.cwd or "", req.session_id or "",
                                 context=req.page)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return StreamingResponse(agents.stream(run), media_type="application/x-ndjson",
                             headers={"X-Run-Id": run.id})


@app.get("/agent/run/{run_id}", dependencies=[Depends(require_token)])
async def agent_run(run_id: str, follow: bool = False, from_index: int = 0):
    """Reattach to a run: replay its events, optionally keep following."""
    run = agents.RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"没有这个 run：{run_id}")
    if follow:
        return StreamingResponse(agents.stream(run, from_index),
                                 media_type="application/x-ndjson")
    return {"ok": True, **run.summary(), "events": run.events[from_index:]}


@app.get("/agent/runs", dependencies=[Depends(require_token)])
async def agent_runs():
    return {"ok": True, "runs": [r.summary() for r in agents.RUNS.values()]}


@app.post("/agent/run/{run_id}/stop", dependencies=[Depends(require_token)])
async def agent_stop(run_id: str):
    return {"ok": agents.stop(run_id)}


@app.get("/journal", dependencies=[Depends(require_token)])
async def journal_search(q: str = "", host: str = "", limit: int = 10, all: bool = False):
    """What has already been run here. The point of the journal is that the next
    agent looks this up *before* writing JS from scratch."""
    return {"ok": True, "stats": journal.stats(),
            "matches": journal.search(q, host, limit, only_ok=not all)}


# --------------------------------------------------------------------------- #
# sites — register a site at runtime (no manifest edit, no extension reload)
# --------------------------------------------------------------------------- #
class SiteReq(BaseModel):
    match: list[str]
    home: Optional[str] = None
    adapter: Optional[str] = None


@app.get("/sites", dependencies=[Depends(require_token)])
async def list_sites():
    return {"ok": True, "sites": config.SITES}


@app.put("/site/{name}", dependencies=[Depends(require_token)])
async def put_site(name: str, req: SiteReq):
    """Add/update a named site. Content scripts already match <all_urls>, so a
    new site needs no manifest change — only this entry, which tells the SW how
    to find (or open) the right tab."""
    entry = {"match": req.match}
    if req.home:
        entry["home"] = req.home
    if req.adapter:
        entry["adapter"] = req.adapter
    config.SITES[name] = entry
    config.save_sites()
    return {"ok": True, "site": name, "entry": entry, "sites": list(config.SITES)}


@app.delete("/site/{name}", dependencies=[Depends(require_token)])
async def delete_site(name: str):
    existed = config.SITES.pop(name, None) is not None
    if existed:
        config.save_sites()
    return {"ok": existed, "sites": list(config.SITES)}


@app.post("/reload", dependencies=[Depends(require_token)])
async def reload_ext():
    """Tell the extension to reload itself from disk (unpacked → re-reads source).
    Enables automated iteration after the one-time manual load."""
    data = await hub.command("reload", {}, timeout=10, serialize=False)
    return JSONResponse({"ok": True, **data})


@app.get("/")
async def root():
    return {"service": "web-bridge", "endpoints": ["/health", "/exec", "/capabilities", "/capability/{id}", "/adapter/{site}/{method}", "/tabs", "/open", "/close", "/sites", "/ws/ext"]}


def _port_in_use() -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((config.HOST, config.PORT)) == 0


def _existing_is_web_bridge() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{config.HOST}:{config.PORT}/health", timeout=2) as r:
            return json.loads(r.read().decode()).get("ok") is True
    except Exception:  # noqa: BLE001
        return False


def main():
    # Uvicorn's bind failure is a traceback that scrolls past, and under launchd
    # it lands in a log nobody reads — the symptom people actually see is "the
    # extension disconnected". So decide explicitly who owns the port, and say so.
    if _port_in_use():
        if _existing_is_web_bridge():
            print(f"[web-bridge] 端口 {config.PORT} 已被另一个 web-bridge 占用，本进程退出（不是错误）")
            raise SystemExit(0)          # clean exit → launchd will NOT restart this
        print(f"[web-bridge] 端口 {config.PORT} 被别的程序占用（lsof -i :{config.PORT} 看是谁）；"
              f"腾出端口后会自动重试", file=sys.stderr)
        raise SystemExit(1)              # non-zero → launchd retries after ThrottleInterval
    restored = agents.restore_runs()
    if restored:
        print(f"[web-bridge] 恢复了 {restored} 条 agent 运行记录")
    print(f"[web-bridge] {time.strftime('%Y-%m-%d %H:%M:%S')} v{VERSION} "
          f"code:{BUILD_AT_START['code_sha256']} pid:{os.getpid()} "
          f"http://{config.HOST}:{config.PORT}  ws:/ws/ext  token:{'set' if config.TOKEN else 'MISSING'}")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning", ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()

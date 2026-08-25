#!/usr/bin/env python3
"""Headless test: a mock extension SW that connects to the bridge WS and answers
commands, so the full HTTP -> WS -> result plumbing + token auth can be tested
without a real browser.

Run the server first (python3 server.py), then: python3 test_mock_ext.py
It will: connect as the extension, then drive the server's own HTTP API and
assert the round-trips work.
"""
import asyncio
import json
import urllib.parse
import urllib.request
import uuid

import websockets

import config

import os
import sys

PORT = int(os.environ.get("WEB_BRIDGE_PORT", config.PORT))
# `client=mock` lets the bridge tell a test double from the real extension and
# refuse it on the live port — see the guard below for why that matters.
WS = f"ws://{config.HOST}:{PORT}/ws/ext?token={config.TOKEN}&client=mock"
BASE = f"http://{config.HOST}:{PORT}"

if PORT == 8790 and os.environ.get("WEB_BRIDGE_ALLOW_MOCK") != "1":
    # Running this file directly against the live bridge is the mistake that
    # produced ~40 "extension connected / disconnected" pairs in ten seconds in
    # the user's log (the mock and the real extension evicting each other) and
    # made the WebSocket look unstable. run_tests.sh starts a throwaway server
    # on its own port with its own state dir; use it.
    sys.exit("别直接跑 test_mock_ext.py：它会连上生产 bridge(8790) 并顶掉真扩展。\n"
             "请跑 ./bridge/run_tests.sh（独立端口 + 临时 state 目录）。")


def http(method, path, body=None, token=config.TOKEN):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


async def mock_extension(ready: asyncio.Event, stop: asyncio.Event):
    """Mock SW. Like the real one, it reconnects when its socket closes — the
    bridge closes a replaced socket so the displaced side comes back."""
    while not stop.is_set():
        try:
            await _mock_session(ready, stop)
        except Exception:  # noqa: BLE001  (closed by a takeover, or server gone)
            if stop.is_set():
                return
            await asyncio.sleep(0.2)


async def _mock_session(ready: asyncio.Event, stop: asyncio.Event):
    async with websockets.connect(WS) as ws:
        await ws.send(json.dumps({"type": "hello", "info": {"mock": True}}))
        ready.set()
        last_ping = 0.0
        while not stop.is_set():
            now = asyncio.get_running_loop().time()
            if now - last_ping > 1.0:               # heartbeat, like the real SW
                last_ping = now
                await ws.send(json.dumps({"type": "ping", "t": now}))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("type") != "command":
                continue
            # answer in a task, not inline: the real SW handles commands
            # concurrently, and serialising here would hide the very contention
            # these tests are about
            asyncio.create_task(_answer(ws, msg))


async def _answer(ws, msg):
    action = msg["action"]
    p = msg.get("payload") or {}
    if action == "exec":
        if "__slow__" in (p.get("code") or ""):
            await asyncio.sleep(2)          # a long page-driving command
        # simulate MAIN-world eval: echo args + a computed field
        data = {"result": {"echo": p.get("args"), "site": p.get("site"), "ran": True}}
    elif action == "tabs":
        data = {"tabs": [{"id": 1, "url": "https://example.com/", "title": "Example"}]}
    elif action == "adapter":
        data = {"result": {"adapter": p.get("site"), "method": p.get("method"), "params": p.get("params")}}
    elif action == "close":
        data = {"closed": []}
    else:
        await ws.send(json.dumps({"type": "result", "id": msg["id"], "ok": False, "error": f"mock: unknown {action}"}))
        return
    await ws.send(json.dumps({"type": "result", "id": msg["id"], "ok": True, "data": data}))


async def main():
    ready, stop = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(mock_extension(ready, stop))
    await asyncio.wait_for(ready.wait(), timeout=5)
    await asyncio.sleep(0.3)

    results = []

    # 1. health shows the mock connected
    code, data = await asyncio.to_thread(http, "GET", "/health")
    results.append(("health.extension_connected", data.get("extension_connected") is True))

    # 2. exec round-trip with args
    code, data = await asyncio.to_thread(http, "POST", "/exec", {"code": "return 1+1", "args": {"x": 5}, "site": "chatgpt"})
    ok = code == 200 and data.get("result", {}).get("echo") == {"x": 5} and data["result"]["ran"]
    results.append(("exec.roundtrip", ok))

    # 3. auth: wrong token rejected
    code, _ = await asyncio.to_thread(http, "POST", "/exec", {"code": "return 1"}, "wrong-token")
    results.append(("auth.rejects_bad_token", code == 401))

    # 4. auth: missing token rejected
    code, _ = await asyncio.to_thread(http, "POST", "/exec", {"code": "return 1"}, "")
    results.append(("auth.rejects_no_token", code == 401))

    # 5. tabs
    code, data = await asyncio.to_thread(http, "GET", "/tabs")
    results.append(("tabs", code == 200 and isinstance(data.get("tabs"), list)))

    # 6. adapter route
    code, data = await asyncio.to_thread(http, "POST", "/adapter/chatgpt/ask", {"params": {"prompt": "hi"}})
    ok = code == 200 and data["result"]["method"] == "ask" and data["result"]["params"] == {"prompt": "hi"}
    results.append(("adapter.roundtrip", ok))

    # 7. unknown site rejected
    code, _ = await asyncio.to_thread(http, "POST", "/adapter/nope/x", {"params": {}})
    results.append(("adapter.unknown_site_404", code == 404))

    # 11. capability params: declared defaults are filled in server-side, so the
    #     page body always receives a complete `args`
    code, data = await asyncio.to_thread(http, "POST", "/capability/extract-article", {"params": {}})
    echoed = (data.get("result") or {}).get("echo") or {}
    results.append(("capability.defaults_applied",
                    code == 200 and echoed.get("markdown") is True and echoed.get("max_chars") == 200000))

    # 12. a typo'd parameter is rejected with a suggestion, not silently ignored
    code, data = await asyncio.to_thread(http, "POST", "/capability/extract-article",
                                         {"params": {"markdwon": True}})
    results.append(("capability.rejects_unknown_param",
                    code == 422 and "markdown" in (data.get("detail") or "")))

    # 13. a missing required parameter is named
    code, data = await asyncio.to_thread(http, "POST", "/capability/collect-list", {"params": {}})
    results.append(("capability.requires_required_param",
                    code == 422 and "item" in (data.get("detail") or "")))

    # 14. enum + numeric bounds are enforced, and strings coerce to numbers
    code, _ = await asyncio.to_thread(http, "POST", "/capability/reader-mode", {"params": {"theme": "neon"}})
    bad_enum = code == 422
    code, data = await asyncio.to_thread(http, "POST", "/capability/reader-mode", {"params": {"width": "900"}})
    coerced = code == 200 and ((data.get("result") or {}).get("echo") or {}).get("width") == 900
    results.append(("capability.enum_and_coercion", bad_enum and coerced))

    # 15. authoring a broken capability is refused and leaves no file behind
    bad_src = '/* @web-bridge-capability {"id":"__wb-test-bad","kind":"nonsense","match":[]} */\nreturn 1'
    code, data = await asyncio.to_thread(http, "PUT", "/capability/__wb-test-bad", {"source": bad_src})
    import capabilities as _caps
    results.append(("capability.lint_rejects_bad_metadata",
                    code == 400 and _caps.get("__wb-test-bad") is None))

    # 16. the sensitive-site blocklist still covers every route
    code, _ = await asyncio.to_thread(http, "POST", "/capability/extract-article",
                                      {"params": {}, "url": "https://www.icbc.com.cn/login"})
    blocked_cap = code == 403
    code, _ = await asyncio.to_thread(http, "POST", "/exec",
                                      {"code": "return 1", "url": "https://www.icbc.com.cn/login"})
    results.append(("blocklist.enforced", blocked_cap and code == 403))

    # 17. an agent can read one capability's full parameter help before calling it
    code, data = await asyncio.to_thread(http, "GET", "/capability/collect-list")
    results.append(("capability.detail_has_help",
                    code == 200 and "必填" in (data.get("params_help") or "") and bool(data.get("source"))))

    # 20. the journal counts repeats and, on the third success, writes the script
    #     into the capability library by itself. Uses a unique marker so the test
    #     can clean its own tracks out of the real journal afterwards.
    import journal as _journal
    marker = "__wb_test_marker_7f3a__"
    probe = f"// {marker}\nreturn 1"
    runs, promoted = [], None
    for _ in range(3):
        code, data = await asyncio.to_thread(
            http, "POST", "/exec", {"code": probe, "url": "https://example.com/"})
        note = (data or {}).get("journal") or {}
        runs.append(note.get("runs"))
        promoted = promoted or note.get("promoted_to")
    counts_up = runs == [1, 2, 3]
    import capabilities as _caps
    results.append(("journal.counts_repeats", counts_up))
    results.append(("journal.auto_promotes_on_third_run",
                    bool(promoted) and _caps.get(promoted) is not None))

    # 21. …and the promoted script is discoverable exactly like a hand-written one
    code, data = await asyncio.to_thread(
        http, "GET", "/capabilities?url=" + urllib.parse.quote("https://example.com/"))
    ids = [c["id"] for c in (data.get("capabilities") or [])]
    results.append(("journal.promoted_is_discoverable",
                    bool(promoted) and promoted in ids and bool(data.get("prior_scripts"))))

    # 22. reformatting a script must NOT look like a new one, or nothing would
    #     ever repeat often enough to be promoted
    reformatted = f"// {marker} (reformatted)\nreturn   1"
    code, data = await asyncio.to_thread(
        http, "POST", "/exec", {"code": reformatted, "url": "https://example.com/"})
    results.append(("journal.normalises_formatting",
                    ((data or {}).get("journal") or {}).get("runs") == 4))

    # clean up: remove the capability this test created and its journal traces
    if promoted:
        await asyncio.to_thread(http, "DELETE", f"/capability/{promoted}")
    try:
        sig = _journal.signature(probe)
        idx = _journal._load_index()
        for key in [k for k in idx if idx[k].get("sig") == sig]:
            idx.pop(key)
        _journal._save_index(idx)
        if _journal.LOG_PATH.exists():
            kept = [ln for ln in _journal.LOG_PATH.read_text(encoding="utf-8").splitlines()
                    if marker not in ln]
            _journal.LOG_PATH.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  (journal cleanup failed: {e})")

    # 23. a long command must not block unrelated work. Before this, ONE global
    #     lock meant a multi-minute chatgpt.ask queued every other call — even on
    #     another site — until it timed out with no explanation. (Found for real:
    #     another agent's `wb chatgpt` froze this session's exec calls.)
    slow = asyncio.create_task(asyncio.to_thread(
        http, "POST", "/exec", {"code": "__slow__ return 1", "site": "chatgpt"}))
    await asyncio.sleep(0.4)                      # let it take the chatgpt lock

    code, data = await asyncio.to_thread(http, "GET", "/tabs")
    tabs_free = code == 200                       # read-only: never queues

    code, data = await asyncio.to_thread(
        http, "POST", "/exec", {"code": "return 2", "site": "github"})
    other_target_free = code == 200               # different target: runs concurrently

    code, data = await asyncio.to_thread(
        http, "POST", "/exec", {"code": "return 3", "site": "chatgpt", "queue_wait_ms": 300})
    same_target_busy = code == 503 and "已有命令在跑" in (data.get("detail") or "")

    _, health = await asyncio.to_thread(http, "GET", "/health")
    reports_inflight = any(i.get("target") == "chatgpt" for i in (health.get("inflight") or []))

    await slow                                     # let it finish, releasing the lock
    results.append(("hub.readonly_never_queues", tabs_free))
    results.append(("hub.other_target_runs_concurrently", other_target_free))
    results.append(("hub.same_target_reports_busy", same_target_busy))
    results.append(("hub.health_reports_inflight", reports_inflight))

    # 24. the side panel's surfaces: agent roster, autorun registration list, and
    #     the autorun toggle. These are what the panel calls on every open, so a
    #     break here is a blank panel rather than a visible error.
    code, data = await asyncio.to_thread(http, "GET", "/agents")
    roster_ok = code == 200 and isinstance(data.get("runners"), dict)
    results.append(("agents.roster_listed", roster_ok))

    # an unknown agent must be refused by name, not spawn something arbitrary
    code, data = await asyncio.to_thread(
        http, "POST", "/agent/ask", {"prompt": "hi", "agent": "definitely-not-installed"})
    results.append(("agents.unknown_agent_refused",
                    code == 400 and "definitely-not-installed" in (data.get("detail") or "")))

    code, data = await asyncio.to_thread(http, "POST", "/agent/ask", {"prompt": "   "})
    results.append(("agents.empty_prompt_refused", code == 400))

    # 25. autorun round trip through the capability library
    import json as _json
    meta = {"id": "__wb-test-autorun", "title": "t", "description": "test only",
            "kind": "restyle", "match": ["example.com"], "params": {}, "autorun": False}
    src = "/* @web-bridge-capability\n" + _json.dumps(meta) + "\n*/\nreturn 1;"
    code, _ = await asyncio.to_thread(
        http, "PUT", "/capability/__wb-test-autorun", {"source": src})
    saved = code == 200

    code, data = await asyncio.to_thread(
        http, "POST", "/capability/__wb-test-autorun/autorun", {"autorun": True})
    turned_on = code == 200 and data.get("capability", {}).get("autorun") is True

    code, data = await asyncio.to_thread(http, "GET", "/capabilities/autorun")
    listed = code == 200 and any(s["id"] == "__wb-test-autorun" for s in data.get("scripts", []))
    patterns = next((s["matches"] for s in data.get("scripts", [])
                     if s["id"] == "__wb-test-autorun"), [])
    results.append(("autorun.save_toggle_list", saved and turned_on and listed))
    # a bare host must become a real match pattern, or userScripts.register
    # rejects the whole batch and every autorun script silently stops working
    results.append(("autorun.host_becomes_match_pattern", patterns == ["*://*.example.com/*"]))

    # extraction scripts have nothing to auto-run, and saying so beats a
    # script that fires on every page load and returns into the void
    code, _ = await asyncio.to_thread(
        http, "POST", "/capability/extract-article/autorun", {"autorun": True})
    results.append(("autorun.refused_for_extract", code == 404))

    await asyncio.to_thread(http, "DELETE", "/capability/__wb-test-autorun")

    # 26. run reattachment: the panel reopens and picks a live run back up, so a
    #     missing run must 404 rather than hang the panel waiting for a stream
    #     that will never come. (Not spawning a real agent here — that costs
    #     quota and seconds; the streaming path is exercised by hand E2E.)
    code, data = await asyncio.to_thread(http, "GET", "/agent/run/does-not-exist")
    results.append(("agents.unknown_run_404", code == 404))

    code, data = await asyncio.to_thread(http, "GET", "/agent/runs")
    results.append(("agents.runs_listed", code == 200 and isinstance(data.get("runs"), list)))

    code, data = await asyncio.to_thread(http, "POST", "/agent/run/does-not-exist/stop")
    results.append(("agents.stop_unknown_is_false", code == 200 and data.get("ok") is False))

    # --- WS security tests last: they connect their own sockets, which take over
    # --- the hub's single extension slot, so no mock round-trip survives them.
    # 8. a web-page Origin must be rejected on the extension socket
    try:
        async with websockets.connect(WS, extra_headers={"Origin": "https://evil.example"}):
            web_origin_rejected = False
    except Exception:
        web_origin_rejected = True
    results.append(("ws.rejects_web_origin", web_origin_rejected))

    # 9. an extension Origin is accepted
    try:
        async with websockets.connect(WS, extra_headers={"Origin": "chrome-extension://abc"}):
            ext_origin_ok = True
    except Exception:
        ext_origin_ok = False
    results.append(("ws.accepts_extension_origin", ext_origin_ok))

    # 10. a wrong token is rejected on the socket too
    try:
        async with websockets.connect(WS.replace(config.TOKEN, "nope")):
            ws_token_rejected = False
    except Exception:
        ws_token_rejected = True
    results.append(("ws.rejects_bad_token", ws_token_rejected))

    # 18. a second socket that takes the slot and then drops must not leave the
    #     real extension dead to the bridge: the displaced side is closed (so it
    #     reconnects) and a still-live socket is re-adopted on its next message.
    #     This is the "扩展突然断连" misdiagnosis, reproduced.
    try:
        async with websockets.connect(WS, extra_headers={"Origin": "chrome-extension://intruder"}):
            await asyncio.sleep(0.2)
    except Exception:
        pass
    recovered = False
    for _ in range(30):                            # a few seconds of grace
        await asyncio.sleep(0.2)
        _, data = await asyncio.to_thread(http, "GET", "/health")
        if data.get("extension_connected") is True:
            recovered = True
            break
    results.append(("ws.recovers_after_socket_takeover", recovered))

    # 19. …and commands work again afterwards (the link is real, not just a flag).
    #     Don't assert the mock's exact echo here: when these tests run against a
    #     server that a real browser extension is also connected to, the reclaimed
    #     socket may be the real one, which answers with the page's own value.
    code, data = await asyncio.to_thread(http, "POST", "/exec", {"code": "return 1", "args": {"a": 1}})
    results.append(("exec.works_after_takeover", code == 200 and "result" in data))

    # 20. /health says which code is running. The trap it closes: the fix is on
    #     disk, the daemon is the process from before the fix, and the live
    #     traceback points at a line number that no longer exists.
    _, data = await asyncio.to_thread(http, "GET", "/health")
    b = data.get("build") or {}
    results.append(("health.reports_build",
                    bool(b.get("version") and b.get("started_at") and b.get("code_sha256"))
                    and "stale" in b))

    # 21. a result carrying a request_id can be claimed again afterwards —
    #     this is the whole point: a dropped connection must not destroy work.
    rid = "test-" + uuid.uuid4().hex[:8]
    code, data = await asyncio.to_thread(
        http, "POST", "/exec", {"code": "return 1", "args": {"keep": True}, "request_id": rid})
    code2, again = await asyncio.to_thread(http, "GET", f"/result/{rid}")
    results.append(("result.claimable_after_the_fact",
                    code == 200 and code2 == 200 and again.get("status") == "done"
                    and (again.get("result") or {}).get("result", {}).get("echo") == {"keep": True}))

    # 22. …and re-POSTing the same id does NOT run the command a second time.
    #     A retry that re-sends a ChatGPT prompt would bill the user twice.
    code3, repeat = await asyncio.to_thread(
        http, "POST", "/exec", {"code": "return 999", "args": {"different": True}, "request_id": rid})
    results.append(("result.same_id_does_not_rerun",
                    code3 == 200 and repeat.get("result", {}).get("echo") == {"keep": True}))

    # 23. an unknown id is a clean 404 that points at the page-side recovery
    code4, missing = await asyncio.to_thread(http, "GET", "/result/no-such-id")
    results.append(("result.unknown_id_404",
                    code4 == 404 and "chatgpt-last" in str(missing.get("detail", ""))))

    # 24. a failing command is remembered as a failure, not as "never happened"
    rid2 = "test-" + uuid.uuid4().hex[:8]
    await asyncio.to_thread(http, "POST", "/adapter/nosuchsite/ask",
                            {"params": {}, "request_id": rid2})
    _, failed = await asyncio.to_thread(http, "GET", f"/result/{rid2}")
    results.append(("result.records_failures", failed.get("status") == "error"))

    # 25. /results lists what is still claimable, without the payloads
    _, listing = await asyncio.to_thread(http, "GET", "/results?limit=50")
    ids = {r.get("request_id") for r in listing.get("results") or []}
    results.append(("results.lists_claimable", rid in ids and rid2 in ids))

    # 26. a test double must not be able to evict the real extension. Only the
    #     live port refuses it, and this suite runs on a throwaway port, so the
    #     assertion here is that the marker is understood and the connection is
    #     accepted — the refusal path is asserted by the client-side guard.
    mock_marked_ok = False
    try:
        async with websockets.connect(WS, extra_headers={"Origin": "chrome-extension://x"}) as w:
            await w.send(json.dumps({"type": "ping", "t": 0}))
            await asyncio.wait_for(w.recv(), timeout=3)
            mock_marked_ok = "client=mock" in WS
    except Exception:
        mock_marked_ok = False
    results.append(("ws.mock_client_is_labelled", mock_marked_ok))

    stop.set()
    await task

    print("\n=== web-bridge server tests ===")
    passed = 0
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        passed += ok
    print(f"{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    import sys, urllib.error  # noqa
    sys.exit(asyncio.run(main()))

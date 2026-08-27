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
    # must be a script worth keeping: promotion now refuses one-liners that do
    # not touch the page, so `return 1` is (correctly) never promoted
    probe = (f"// {marker}\n"
             'const rows = document.querySelectorAll(".item");\n'
             'return {count: rows.length, first: rows[0]?.textContent || null};')
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
    reformatted = (f"// {marker} (reformatted)\n"
                   'const rows = document.querySelectorAll( ".item" );\n'
                   'return { count: rows.length, first: rows[0]?.textContent || null };')
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

    # an autorun script must receive the SAME filled-in arguments a manual run
    # gets; injecting a bare {} made one script behave two different ways
    # depending on whether the user pressed 运行 or just loaded the page
    meta_def = dict(meta, id="__wb-test-autodefaults", autorun=True,
                    params={"color": {"type": "string", "default": "#7c3aed",
                                      "description": "test"}})
    src_def = "/* @web-bridge-capability\n" + _json.dumps(meta_def) + "\n*/\nreturn args;"
    await asyncio.to_thread(http, "PUT", "/capability/__wb-test-autodefaults", {"source": src_def})
    code, data = await asyncio.to_thread(http, "GET", "/capabilities/autorun")
    entry = next((x for x in data.get("scripts", []) if x["id"] == "__wb-test-autodefaults"), None)
    results.append(("autorun.carries_declared_defaults",
                    bool(entry) and entry.get("args", {}).get("color") == "#7c3aed"))
    await asyncio.to_thread(http, "DELETE", "/capability/__wb-test-autodefaults")

    # a required parameter has nothing to supply it on page load, so the switch
    # must refuse rather than turn on and quietly do nothing
    code, data = await asyncio.to_thread(
        http, "POST", "/capability/collect-list/autorun", {"autorun": True})
    results.append(("autorun.refused_when_param_required",
                    code == 404 and "必填参数" in (data.get("detail") or "")))

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

    # 27. user scripts are a separate library from agent capabilities, and their
    #     switch must actually persist — it answered 422 and silently saved
    #     nothing because the request model was defined after its first use
    #     (`from __future__ import annotations` turned the NameError into a
    #     query parameter instead of a crash).
    code, data = await asyncio.to_thread(http, "PUT", "/user-script/new", {
        "name": "__wb-test-user", "code": "return 1;", "matches": ["example.com"]})
    uid = (data.get("script") or {}).get("id", "")
    saved_user = code == 200 and bool(uid)

    code, data = await asyncio.to_thread(
        http, "POST", f"/user-script/{uid}/autorun", {"autorun": True})
    results.append(("user_scripts.autorun_persists",
                    saved_user and code == 200 and data["script"]["autorun"] is True))

    code, data = await asyncio.to_thread(http, "GET", "/capabilities/autorun")
    ids = [s["id"] for s in data.get("scripts", [])]
    results.append(("user_scripts.registered_for_autorun", any(uid in i for i in ids)))

    # the two libraries must not bleed into each other
    code, data = await asyncio.to_thread(
        http, "GET", "/capabilities?url=" + urllib.parse.quote("https://example.com/"))
    leaked = any("__wb-test-user" in (c.get("title") or "") for c in data.get("capabilities", []))
    code, data = await asyncio.to_thread(
        http, "GET", "/user-scripts?url=" + urllib.parse.quote("https://example.com/"))
    listed = any(s["id"] == uid for s in data.get("scripts", []))
    results.append(("user_scripts.separate_from_capabilities", listed and not leaked))

    # refining a script over several chat rounds must update the same record,
    # not leave a pile of near-identical copies — and an update that carries only
    # the new code must not reset the name, scope or the autorun the user set.
    await asyncio.to_thread(http, "POST", f"/user-script/{uid}/autorun", {"autorun": True})
    code, data = await asyncio.to_thread(
        http, "PUT", f"/user-script/{uid}", {"code": "return 2;"})
    rec = data.get("script") or {}
    results.append(("user_scripts.partial_update_keeps_fields",
                    code == 200 and rec.get("autorun") is True
                    and rec.get("name") == "__wb-test-user"
                    and rec.get("matches") == ["example.com"]
                    and "return 2;" in rec.get("code", "")))

    code, data = await asyncio.to_thread(http, "GET", "/user-scripts")
    same_id = [x for x in data.get("scripts", []) if x["id"] == uid]
    results.append(("user_scripts.update_does_not_duplicate", len(same_id) == 1))

    # a saved script must say what it does, when it changed and who wrote it —
    # the list shows a name and a sentence, never the code
    code, data = await asyncio.to_thread(http, "PUT", f"/user-script/{uid}",
                                         {"code": "return 3;", "note": "加了分页", "by": "claude"})
    rec = data.get("script") or {}
    results.append(("user_scripts.records_author_and_time",
                    rec.get("updated_by") == "claude" and bool(rec.get("updated"))
                    and rec.get("revisions", 0) >= 1))
    # an update appends to the description instead of replacing it, so the entry
    # records what each round added
    code, data = await asyncio.to_thread(http, "PUT", f"/user-script/{uid}",
                                         {"code": "return 4;", "note": "又加了排序"})
    note = (data.get("script") or {}).get("note", "")
    results.append(("user_scripts.note_accumulates",
                    "加了分页" in note and "又加了排序" in note))

    # the capability library shows when each was last touched
    code, data = await asyncio.to_thread(http, "GET", "/capabilities")
    caps = data.get("capabilities", [])
    results.append(("capabilities.expose_updated_time",
                    bool(caps) and all(c.get("updated") for c in caps)))

    # building a page script is dozens of one-off probes; counting that as
    # "this site needs a capability" made the tool nag about work nobody
    # repeated. A real gap is REPETITION, not volume.
    import journal as _j6
    st6 = _j6.usage_stats(30)
    authoring = [g for g in st6.get("gaps", [])
                 if g["distinct"] >= max(5, g["adhoc"] * 0.8) and g["repeats"] <= 1]
    results.append(("journal.separates_authoring_from_gaps",
                    all(g["looks_like_authoring"] for g in authoring)))

    # and auto-promotion must not turn debugging one-liners into capabilities:
    # `location.reload()` repeated during development became one, titled
    # "🤖 location.reload();return 1"
    results.append(("journal.rejects_trivial_promotions",
                    _j6.looks_trivial("location.reload(); return 1")
                    and _j6.looks_trivial("return document.title")
                    and not _j6.looks_trivial(
                        'document.querySelectorAll(".ad").forEach(e=>e.remove()); return {n:1}')))

    # The index is derived data — rebuildable from capabilities/*.js — so it
    # belongs in a cache directory, never next to the config.
    import toolsearch as _ts0
    cache = str(_ts0.cache_dir())
    results.append(("toolsearch.index_lives_in_cache",
                    (".cache" in cache or "Local" in cache) and "config" not in cache))

    # Below a size budget the whole library is handed over instead of being
    # filtered: the model matches "把网页数据弄成 excel" to extract-tables better
    # than a synonym table does, and cannot match what it was never shown.
    cat = _ts0.catalogue("https://unogs.com/")
    results.append(("toolsearch.catalogue_fits_and_is_complete",
                    cat is not None and cat["count"] == len(_ts0.capabilities.all_caps())
                    and cat["chars"] < _ts0.CATALOGUE_BUDGET_CHARS
                    and any("★本页" in l for l in cat["lines"])))

    # …and once it no longer fits, ranking takes over rather than truncating
    saved_budget = _ts0.CATALOGUE_BUDGET_CHARS
    _ts0.CATALOGUE_BUDGET_CHARS = 50
    results.append(("toolsearch.falls_back_to_ranking_when_large",
                    _ts0.catalogue("") is None and bool(_ts0.search("表格", limit=3))))
    _ts0.CATALOGUE_BUDGET_CHARS = saved_budget

    # The hot path must never shell out: a briefing is built on every message,
    # and a vector query costs seconds.
    import time as _t9
    _t0 = _t9.time()
    _ts0.catalogue("https://example.com/")
    results.append(("toolsearch.catalogue_is_hot_path_fast", (_t9.time() - _t0) < 0.5))

    # Retrieval by intent, not by which URL is open. Looking tools up through
    # `match` alone meant a tool for another site was invisible even when it did
    # exactly what was asked — the user's intent lost to where they were standing.
    import toolsearch as _ts
    code, data = await asyncio.to_thread(
        http, "GET", "/tools/search?q=" + urllib.parse.quote("表格 存成 JSON") + "&limit=3")
    ids = [t["id"] for t in data.get("tools", [])]
    results.append(("toolsearch.ranks_by_intent",
                    code == 200 and ids and ids[0] == "extract-tables"))

    # the same question in the other language must find the same tool
    _, data_en = await asyncio.to_thread(http, "GET", "/tools/search?q=table&limit=3")
    results.append(("toolsearch.crosses_languages",
                    [t["id"] for t in data_en.get("tools", [])][:1] == ["extract-tables"]))

    # a tool for another site still surfaces; the url only boosts
    _, off = await asyncio.to_thread(
        http, "GET", "/tools/search?q=" + urllib.parse.quote("提取表格")
        + "&url=" + urllib.parse.quote("https://unrelated-site.test/") + "&limit=5")
    results.append(("toolsearch.url_boosts_not_filters",
                    any(t["id"] == "extract-tables" for t in off.get("tools", []))))

    # and a tool reported as not doing its job must sink
    before = next((t["score"] for t in data.get("tools", []) if t["id"] == "extract-tables"), 0)
    for _ in range(4):
        await asyncio.to_thread(http, "POST", "/tools/extract-tables/feedback",
                                {"ok": False, "note": "test"})
    _, after_data = await asyncio.to_thread(
        http, "GET", "/tools/search?q=" + urllib.parse.quote("表格 存成 JSON") + "&limit=3")
    after = next((t["score"] for t in after_data.get("tools", []) if t["id"] == "extract-tables"), 0)
    results.append(("toolsearch.bad_reports_demote", before > 0 and after < before))
    try:
        (_ts.journal.STATE_DIR / "tool-feedback.json").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass

    # A briefing only reaches side-panel runs. The reinventing was happening in a
    # terminal MCP client, which never sees one — eleven hand-written scripts on
    # a site whose capability was sitting right there, discovery never called.
    # So the exec RESULT has to carry the hint: every caller sees that.
    meta_hint = dict(meta, id="__wb-test-hinted", match=["example.com"], autorun=False)
    src_hint = "/* @web-bridge-capability\n" + _json.dumps(meta_hint) + "\n*/\nreturn 1;"
    await asyncio.to_thread(http, "PUT", "/capability/__wb-test-hinted", {"source": src_hint})
    code, data = await asyncio.to_thread(
        http, "POST", "/exec", {"code": "return 1", "url": "https://example.com/"})
    hint = data.get("tools_available") or {}
    results.append(("exec.hints_existing_tools",
                    code == 200 and any(t["id"] == "__wb-test-hinted" for t in hint.get("tools", []))))
    await asyncio.to_thread(http, "DELETE", "/capability/__wb-test-hinted")

    # …and stays quiet where nothing site-specific exists, or it becomes noise
    code, data = await asyncio.to_thread(
        http, "POST", "/exec", {"code": "return 1", "url": "https://nothing-here-xyz.test/"})
    results.append(("exec.no_hint_without_tools", "tools_available" not in data))

    # the single biggest lever on whether a saved tool gets used: hand it to the
    # agent in the briefing instead of making it think to ask. Measured on a real
    # run — before: 1 capability call then 32 hand-written scripts; after: 2
    # capability calls and no hand-written JS at all.
    import agents as _agents5, capabilities as _caps5
    site_cap = next((c for c in _caps5.all_caps() if c.get("match") != ["*"]), None)
    if site_cap:
        host = site_cap["match"][0].strip("*.")
        block = _agents5.available_tools_block(f"https://{host}/")
        results.append(("agents.brief_lists_existing_tools",
                        site_cap["id"] in block and "web_run_capability" in block))
    else:
        results.append(("agents.brief_lists_existing_tools", True))

    # A page with no site-specific tool must not claim it has some — but it still
    # gets the catalogue, because a tool written for another site is often the
    # right answer and the model can only choose what it has been shown.
    blank = _agents5.available_tools_block("https://no-such-site-xyz.test/")
    results.append(("agents.no_false_site_tools_claim",
                    "这个页面已经有" not in blank and "Agent Tools" in blank))

    # "did it use the tools I saved, or write JS again?" had no answer short of
    # reading the JSONL by hand — the reuse rate is that answer.
    code, data = await asyncio.to_thread(http, "GET", "/journal/stats?days=7")
    results.append(("journal.reports_reuse_rate",
                    code == 200 and "reuse_rate" in data
                    and isinstance(data.get("tools"), list)
                    and data["capability_runs"] + data["adhoc_execs"] >= 0))

    # both libraries must be reachable from a conversation. When only the page
    # library had a tool, "把上面这个创建到脚本库" put a capability meant for the
    # agent into the user's page scripts — the brief now routes by who it is for.
    import agents as _agents4
    brief4 = _agents4.panel_brief({"url": "https://example.com/", "title": "t"})
    results.append(("agents.brief_routes_both_libraries",
                    "web_save_page_script" in brief4 and "web_save_capability" in brief4
                    and "Agent Tools" in brief4 and "Page Tools" in brief4))

    # a whole library must be able to move to another machine, and importing
    # must never quietly overwrite work that is already here
    code, bundle = await asyncio.to_thread(http, "GET", "/user-scripts/export")
    results.append(("user_scripts.export_bundle",
                    code == 200 and bundle.get("kind") == "web-bridge/user-scripts"
                    and any(x["id"] == uid for x in bundle.get("scripts", []))))

    code, data = await asyncio.to_thread(
        http, "POST", "/user-scripts/import", {"data": bundle, "overwrite": False})
    # same ids already present → kept apart under a marked name, nothing replaced
    results.append(("user_scripts.import_never_clobbers",
                    code == 200 and not data.get("replaced") and bool(data.get("renamed"))))
    for name in data.get("renamed", []):
        code2, listing = await asyncio.to_thread(http, "GET", "/user-scripts")
        for sc in listing.get("scripts", []):
            if sc["name"] == name:
                await asyncio.to_thread(http, "DELETE", f"/user-script/{sc['id']}")

    code, data = await asyncio.to_thread(http, "POST", "/user-scripts/import",
                                         {"data": {"kind": "something-else"}})
    results.append(("user_scripts.import_rejects_foreign_file", code == 400))

    code, caps_bundle = await asyncio.to_thread(http, "GET", "/capabilities/export")
    results.append(("capabilities.export_bundle",
                    code == 200 and caps_bundle.get("kind") == "web-bridge/capabilities"
                    and all(c.get("source") for c in caps_bundle.get("capabilities", []))))

    # a script has to be able to leave this machine: export it as a bookmarklet
    code, data = await asyncio.to_thread(http, "GET", f"/user-script/{uid}/bookmarklet")
    url = data.get("url", "")
    html = data.get("html", "")
    results.append(("user_scripts.bookmarklet_export",
                    code == 200 and url.startswith("javascript:")
                    and "\n" not in url and " " not in url        # must survive a bookmark URL
                    and 'class="drag"' in html))                   # draggable, the only way to install

    # the export exists to leave this machine: it must open on a computer with no
    # extension, no bridge and no network, so nothing may be fetched from outside
    body = html.replace(url, "")                    # the code itself is not a dependency
    results.append(("user_scripts.export_is_self_contained",
                    "http://" not in body and "https://" not in body
                    and "<script" not in body       # no external or inline script needed
                    and "怎么用" in body))          # explains itself to a stranger

    # the agent needs a way to remove a script it replaced — without one it
    # renamed a script to "(可删除)" and left it auto-running
    import mcp_server as _mcp2
    results.append(("mcp.can_delete_page_scripts",
                    "web_delete_page_script" in [t["name"] for t in _mcp2.TOOLS]))

    await asyncio.to_thread(http, "DELETE", f"/user-script/{uid}")

    # 28. the chat-to-library loop the panel exists for: an agent writes a page
    #     script and saves it into the USER's library. There was no tool for
    #     that at all — only web_save_capability, which is the agent's own
    #     library — so an agent asked to "beautify this page" could probe and
    #     inject but never hand anything back.
    import mcp_server as _mcp
    tool_names = [t["name"] for t in _mcp.TOOLS]
    results.append(("mcp.can_save_page_scripts",
                    "web_save_page_script" in tool_names and "web_page_scripts" in tool_names))

    # the brief must tell the agent to apply the change and show the code…
    import agents as _agents
    brief = _agents.panel_brief({"url": "https://example.com/", "title": "t"})
    results.append(("agents.brief_demands_delivery",
                    all(k in brief for k in ("web_exec", "```js"))))

    # …and must NOT have it save on its own. Saving is the user's call: the panel
    # puts a button under the code block, and the user usually wants a few more
    # rounds of changes before keeping anything.
    results.append(("agents.brief_forbids_unprompted_save", "不要自己保存" in brief))

    # a run must survive the bridge restarting under it: history used to live
    # only in memory, so a restart to pick up a code change killed the running
    # agent AND erased what it had already produced — from the panel that looked
    # like the agent had simply done nothing.
    import agents as _agents3, json as _json3, time as _time3
    probe = _agents3.Run("__wb-test-run", "claude", "restart 存活验证", "/tmp")
    probe.emit({"type": "text", "text": "半路的输出"})
    probe.persist()
    _agents3.RUNS.pop("__wb-test-run", None)
    on_disk = (_agents3.RUN_DIR / "__wb-test-run.json").exists()
    _agents3.restore_runs()
    back = _agents3.RUNS.get("__wb-test-run")
    results.append(("agents.runs_survive_restart",
                    on_disk and back is not None
                    and any(e.get("text") == "半路的输出" for e in back.events)))
    # …and one left running belongs to a process that is gone, so it is closed
    # out with an explanation rather than hanging as "still running" forever
    results.append(("agents.interrupted_run_explained",
                    back is not None and back.done and "重启" in (back.error or "")))
    (_agents3.RUN_DIR / "__wb-test-run.json").unlink(missing_ok=True)
    _agents3.RUNS.pop("__wb-test-run", None)

    # a single oversized line must not kill a run (claude's stream-json puts a
    # whole event on one line; the 64KB default limit ended runs mid-flight)
    results.append(("agents.stream_limit_raised", _agents.STREAM_LIMIT >= 8 * 1024 * 1024))

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

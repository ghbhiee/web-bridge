#!/usr/bin/env python3
"""web-bridge MCP server (stdio, dependency-free).

Exposes the bridge as MCP tools so any MCP client (Claude Code, codex, openclaw,
hermes, dsh, …) can drive logged-in browser pages. Implements the minimal MCP
JSON-RPC-over-stdio protocol directly (newline-delimited messages), so no SDK is
required.

Tools:
  web_status              - bridge + extension health
  web_tabs                - list open browser tabs
  web_open                - open / focus a tab at a URL
  web_exec                - run JS in a page's MAIN world (the generic primitive)
  web_adapter             - call a site adapter method
  web_chatgpt_ask         - ask the logged-in ChatGPT (text; optional new chat)
  web_reload_extension    - reload the extension from disk

Starts the bridge server automatically if it isn't running.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import service  # noqa: E402

BASE = config.base_url()
PROTOCOL_VERSION = "2024-11-05"


# --------------------------------------------------------------------------- #
# bridge HTTP client
# --------------------------------------------------------------------------- #
def _http(method, path, body=None, timeout=320):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if config.TOKEN:
        req.add_header("Authorization", f"Bearer {config.TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"detail": str(e)}
    except urllib.error.URLError as e:
        return 0, {"detail": f"cannot reach bridge ({BASE}): {e.reason}"}


def _server_up():
    return _http("GET", "/health", timeout=3)[0] == 200


def _ensure_server():
    if _server_up():
        return True
    # If the LaunchAgent owns the port, let launchd start it — a server spawned
    # here would lose the race and exit, leaving the caller confused.
    if service.installed() and not service.IS_WINDOWS:
        subprocess.run(["launchctl", "kickstart", service.SERVICE], capture_output=True)
        for _ in range(40):
            if _server_up():
                return True
            time.sleep(0.2)
        return False
    log = open(os.path.join(HERE, "server.log"), "ab")
    try:
        subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=HERE)
    except Exception:
        return False
    for _ in range(40):
        if _server_up():
            return True
        time.sleep(0.2)
    return False


def _post_recoverable(path, body, timeout):
    """POST a page-driving command with a claimable request_id.

    A dropped connection must not destroy work the browser already did: the id
    lets us ask the bridge for the outcome instead of reporting a failure (and
    a retry with the same id attaches to the running command rather than
    starting a second one).
    """
    rid = body.get("request_id") or uuid.uuid4().hex
    body["request_id"] = rid
    code, data = _http("POST", path, body, timeout=timeout)
    if code != 0:
        if isinstance(data, dict):
            data.setdefault("request_id", rid)
        return data
    deadline = time.time() + max(30.0, timeout)
    while time.time() < deadline:
        c, d = _http("GET", f"/result/{urllib.parse.quote(rid)}?wait=20", timeout=40)
        if c == 200 and d.get("status") == "done":
            out = d.get("result") or {}
            return {"ok": True, "recovered": True, "request_id": rid, **out}
        if c == 200 and d.get("status") == "error":
            return {"ok": False, "request_id": rid, "error": d.get("error")}
        if c == 404:
            break
        if c == 0:
            time.sleep(2)
    return {"ok": False, "request_id": rid,
            "error": f"connection to the bridge dropped and no result is cached for {rid}",
            "hint": "for ChatGPT, call web_chatgpt_last — the answer is still on the page"}


# --------------------------------------------------------------------------- #
# tool definitions + dispatch
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        "name": "web_status",
        "description": "Check the web-bridge: is the local server up, is the browser extension connected, which sites are configured.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "web_tabs",
        "description": "List the browser's open tabs (id, url, title). Optional substring filter on url/title.",
        "inputSchema": {"type": "object", "properties": {"filter": {"type": "string"}}},
    },
    {
        "name": "web_open",
        "description": "Open (or focus an existing) browser tab at a URL.",
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string"}, "activate": {"type": "boolean", "default": True}}, "required": ["url"]},
    },
    {
        "name": "web_exec",
        "description": ("Run JavaScript in a page's MAIN world and return its value. `code` is a "
                        "function body: `args` is in scope and you may `return` / `await`. Because it "
                        "runs in the page's own JS world (extension-injected, CSP-exempt), it can read "
                        "page globals, call page functions, and fetch same-origin APIs with the user's "
                        "login cookies. Target a page via `site` (a configured site name) or `url`. "
                        "**Before hand-writing a scraper for a site, call web_capabilities for that "
                        "url** — a purpose-built tool may already exist, and the result of this call "
                        "will tell you (`tools_available`) when one does."),
        "inputSchema": {"type": "object", "properties": {
            "code": {"type": "string", "description": "JS function body; `args` in scope; may return/await"},
            "args": {"description": "JSON value passed as `args`"},
            "site": {"type": "string", "description": "configured site name (e.g. chatgpt)"},
            "url": {"type": "string", "description": "or an explicit URL substring / to open"},
            "new_tab": {"type": "boolean", "default": False},
            "timeout_ms": {"type": "integer", "default": 30000}}, "required": ["code"]},
    },
    {
        "name": "web_adapter",
        "description": "Call a registered site-adapter method (e.g. site=chatgpt method=ask).",
        "inputSchema": {"type": "object", "properties": {
            "site": {"type": "string"}, "method": {"type": "string"},
            "params": {"type": "object"}, "timeout_ms": {"type": "integer", "default": 300000}},
            "required": ["site", "method"]},
    },
    {
        "name": "web_chatgpt_ask",
        "description": ("Ask the user's logged-in ChatGPT web session and return the answer text. Uses "
                        "the real account (history/Plus), no API key or cost. Set new_chat to start fresh."),
        "inputSchema": {"type": "object", "properties": {
            "prompt": {"type": "string"},
            "new_chat": {"type": "boolean", "default": False},
            "timeout_ms": {"type": "integer", "default": 300000}}, "required": ["prompt"]},
    },
    {
        "name": "web_chatgpt_last",
        "description": ("Re-read the LAST answer of the ChatGPT conversation already open in the "
                        "browser, including any generated images. Sends nothing and costs no quota. "
                        "Use it when a web_chatgpt_ask call lost its connection (the page had already "
                        "finished the work), or to pick up an answer produced outside this session."),
        "inputSchema": {"type": "object", "properties": {
            "want_images": {"type": "boolean", "default": True,
                            "description": "download the generated images as base64 too"},
            "timeout_ms": {"type": "integer", "default": 120000}}},
    },
    {
        "name": "web_result",
        "description": ("Claim the result of an earlier command by its request_id — the recovery path "
                        "for a dropped connection. Every page-driving tool here returns a request_id; "
                        "if the call failed to come back, ask for that id instead of re-running the "
                        "work. `wait` long-polls while it is still running. No arguments lists what is "
                        "still claimable."),
        "inputSchema": {"type": "object", "properties": {
            "request_id": {"type": "string"},
            "wait_seconds": {"type": "number", "default": 0}}},
    },
    {
        "name": "web_find_tool",
        "description": ("**Do this before writing a scraper.** Call it with no query to get the "
                        "whole tool library as one-liners (it is small; read it and pick — you "
                        "judge relevance better than a keyword scorer does). Pass a query only "
                        "once the library is too large to list, and it will rank by relevance and "
                        "by whether each tool has actually worked. Either way it is NOT limited to "
                        "the page you are on: a tool built for another site is still the right "
                        "answer when it does what you need."),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "what you are trying to do, in your own words"},
            "url": {"type": "string", "description": "the page you are working on (a boost, optional)"},
            "limit": {"type": "integer", "default": 5}}},
    },
    {
        "name": "web_rate_tool",
        "description": ("Report that a tool did or did not do its job. The journal already sees "
                        "whether a script threw; it cannot see that the answer was wrong or "
                        "useless, and you can. Repeated bad reports sink a tool in web_find_tool's "
                        "ranking, so a plausible-but-broken tool stops being suggested."),
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string", "description": "capability id"},
            "ok": {"type": "boolean", "description": "true if it did the job"},
            "note": {"type": "string", "description": "one line on what was wrong, if it was not"}},
            "required": ["id", "ok"]},
    },
    {
        "name": "web_capabilities",
        "description": ("**Start here for page work.** Lists what can be done on a page: ready-made "
                        "capabilities for extracting data, automating tasks, and restyling/re-laying-out "
                        "pages, each with its description and parameters. Pass `url` (or `site`) to get "
                        "the ones that apply to that page; omit for the whole library. Prefer an existing "
                        "capability over hand-writing JS; if none fits, run `inspect-page` to learn the "
                        "page's structure, then use web_exec or save a new capability."),
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "page URL (or a substring of an open tab's URL)"},
            "site": {"type": "string", "description": "or a configured site name"},
            "capability": {"type": "string", "description": "one capability id → full parameter help "
                                                            "and its source (read before editing it)"}}},
    },
    {
        "name": "web_run_capability",
        "description": ("Run a capability from the library on a page. Get ids and parameter names from "
                        "web_capabilities first. Useful ones: inspect-page (what's scrapable here), "
                        "extract-article (page → markdown), extract-tables, collect-list (paginated "
                        "harvesting), reader-mode (re-layout for reading)."),
        "inputSchema": {"type": "object", "properties": {
            "capability": {"type": "string"},
            "params": {"type": "object", "description": "capability parameters"},
            "url": {"type": "string"}, "site": {"type": "string"},
            "new_tab": {"type": "boolean", "default": False},
            "timeout_ms": {"type": "integer", "default": 120000}}, "required": ["capability"]},
    },
    {
        "name": "web_save_capability",
        "description": ("Add a reusable capability to the library so it is discoverable later (by any "
                        "agent, and in the extension popup). `source` is a JS file: a JSON metadata "
                        "header comment `/* @web-bridge-capability {\"id\",\"title\",\"description\","
                        "\"kind\":\"extract|automate|restyle|inspect\",\"match\":[\"*\"|\"host.com\"],"
                        "\"params\":{…}} */` followed by the body, where `args` holds the params and you "
                        "`return` a JSON-safe value. Use this to turn a one-off script into a permanent "
                        "site skill. Note the bridge already auto-saves any script that succeeds 3 "
                        "times (as `auto-<host>-<sig>` with a machine-written header) — overwriting "
                        "that id with a proper title/description is exactly what this tool is for."),
        "inputSchema": {"type": "object", "properties": {
            "capability": {"type": "string", "description": "id (also the filename)"},
            "source": {"type": "string", "description": "full JS source incl. metadata header"}},
            "required": ["capability", "source"]},
    },
    {
        "name": "web_save_page_script",
        "description": ("Save JS into the USER's page-script library (the panel's 页面 tab), where "
                        "they can re-run, edit and auto-run it. This is where a script the user "
                        "asked you to write belongs — web_save_capability is for your own reusable "
                        "abilities, not for the user's page tweaks. Set autorun for scripts that "
                        "restyle a page and should run on every load. Pass the existing id to "
                        "update one."),
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "short name the user will see"},
            "code": {"type": "string", "description": "function body; may await; may return JSON"},
            "matches": {"type": "array", "items": {"type": "string"},
                        "description": "where it applies: [\"example.com\"], [\"example.com/list\"] or [\"*\"]"},
            "autorun": {"type": "boolean", "default": False,
                        "description": "run it automatically on page load (restyle scripts)"},
            "note": {"type": "string", "description": "one line on what it does"},
            "id": {"type": "string", "description": "omit to create; pass to update"}},
            "required": ["name", "code"]},
    },
    {
        "name": "web_page_scripts",
        "description": ("List the user's own page scripts, optionally for one url. Check here "
                        "before writing a new one — the user may already have it."),
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string"}}},
    },
    {
        "name": "web_delete_page_script",
        "description": ("Delete one of the user's page scripts by id. Needed when you merge several "
                        "scripts into one, or replace a script you wrote earlier — without it the "
                        "old one keeps auto-running alongside the new one."),
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string", "description": "script id from web_page_scripts"}},
            "required": ["id"]},
    },
    {
        "name": "web_journal",
        "description": ("**Look here before writing JS.** Searches what has already been run on a "
                        "site — one-off scripts from web_exec and capability calls — most-used "
                        "first, with the actual code. A script that worked here before beats one "
                        "you invent now. Scripts that succeed 3 times are auto-saved into the "
                        "capability library, so also check web_capabilities."),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "keyword, matched against the summary and the code"},
            "host": {"type": "string", "description": "restrict to one site, e.g. x.com"},
            "limit": {"type": "integer", "default": 10},
            "include_failed": {"type": "boolean", "default": False,
                               "description": "also list scripts that never succeeded here"}}},
    },
    {
        "name": "web_close_tab",
        "description": ("Close browser tabs by URL substring or tab id. Use it to clean up tabs you "
                        "opened for a task. Requires an explicit target — never closes everything."),
        "inputSchema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "substring of the tab URL"},
            "tab_id": {"type": "integer", "description": "or an exact tab id from web_tabs"}}},
    },
    {
        "name": "web_reload_extension",
        "description": "Reload the web-bridge extension from disk (picks up code changes).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(name, args):
    args = args or {}
    if name == "web_status":
        _ensure_server()
        return _http("GET", "/health")[1]
    if name == "web_tabs":
        _ensure_server()
        f = args.get("filter", "")
        return _http("GET", "/tabs" + (f"?filter={urllib.parse.quote(f)}" if f else ""))[1]
    if name == "web_open":
        _ensure_server()
        return _http("POST", "/open", {"url": args["url"], "activate": args.get("activate", True)})[1]
    if name == "web_exec":
        _ensure_server()
        body = {"code": args["code"], "args": args.get("args"), "site": args.get("site"),
                "url": args.get("url"), "new_tab": args.get("new_tab", False),
                "timeout_ms": args.get("timeout_ms", 30000)}
        return _post_recoverable("/exec", body, timeout=body["timeout_ms"] / 1000 + 15)
    if name == "web_adapter":
        _ensure_server()
        t = args.get("timeout_ms", 300000)
        return _post_recoverable(f"/adapter/{args['site']}/{args['method']}",
                                 {"params": args.get("params", {}), "timeout_ms": t}, timeout=t / 1000 + 15)
    if name == "web_chatgpt_ask":
        _ensure_server()
        t = args.get("timeout_ms", 300000)
        return _post_recoverable("/adapter/chatgpt/ask",
                                 {"params": {"prompt": args["prompt"],
                                             "new_chat": args.get("new_chat", False)},
                                  "timeout_ms": t}, timeout=t / 1000 + 15)
    if name == "web_chatgpt_last":
        _ensure_server()
        t = args.get("timeout_ms", 120000)
        return _post_recoverable("/adapter/chatgpt/last",
                                 {"params": {"want_images": args.get("want_images", True)},
                                  "timeout_ms": t}, timeout=t / 1000 + 15)
    if name == "web_result":
        _ensure_server()
        if not args.get("request_id"):
            return _http("GET", "/results?limit=20")[1]
        w = float(args.get("wait_seconds", 0))
        return _http("GET", f"/result/{urllib.parse.quote(args['request_id'])}?wait={w}",
                     timeout=w + 20)[1]
    if name == "web_find_tool":
        _ensure_server()
        qs = [f"limit={int(args.get('limit', 5))}"]
        if args.get("query"):
            qs.append("q=" + urllib.parse.quote(args["query"]))
        if args.get("url"):
            qs.append("url=" + urllib.parse.quote(args["url"]))
        return _http("GET", "/tools/search?" + "&".join(qs))[1]
    if name == "web_rate_tool":
        _ensure_server()
        code, data = _http("POST", f"/tools/{urllib.parse.quote(args['id'])}/feedback",
                           {"ok": bool(args.get("ok")), "note": args.get("note", "")})
        if code != 200:
            return {"ok": False, "error": data.get("detail")}
        return data
    if name == "web_capabilities":
        _ensure_server()
        if args.get("capability"):
            return _http("GET", "/capability/" + urllib.parse.quote(args["capability"]))[1]
        qs = ""
        if args.get("url"):
            qs = "?url=" + urllib.parse.quote(args["url"])
        elif args.get("site"):
            qs = "?site=" + urllib.parse.quote(args["site"])
        return _http("GET", "/capabilities" + qs)[1]
    if name == "web_run_capability":
        _ensure_server()
        t = args.get("timeout_ms", 120000)
        body = {"params": args.get("params", {}), "site": args.get("site"),
                "url": args.get("url"), "new_tab": args.get("new_tab", False), "timeout_ms": t}
        code, data = _http("POST", f"/capability/{args['capability']}", body, timeout=t / 1000 + 15)
        if code == 422:
            # bad arguments — the detail explains how to call it correctly
            return {"ok": False, "error": "参数不合法 / invalid parameters",
                    "detail": data.get("detail"),
                    "hint": "call web_capabilities with this capability id for its full parameter help"}
        return data
    if name == "web_save_capability":
        _ensure_server()
        code, data = _http("PUT", f"/capability/{args['capability']}", {"source": args["source"]})
        if code != 200:
            return {"ok": False, "error": "capability not saved", "detail": data.get("detail")}
        return data
    if name == "web_save_page_script":
        _ensure_server()
        sid = args.get("id") or "new"
        body = {"name": args.get("name", ""), "code": args["code"],
                "matches": args.get("matches") or ["*"],
                "autorun": bool(args.get("autorun")), "note": args.get("note", "")}
        code, data = _http("PUT", f"/user-script/{urllib.parse.quote(sid)}", body)
        if code != 200:
            return {"ok": False, "error": data.get("detail")}
        return data
    if name == "web_page_scripts":
        _ensure_server()
        qs = "?url=" + urllib.parse.quote(args["url"]) if args.get("url") else ""
        return _http("GET", "/user-scripts" + qs)[1]
    if name == "web_delete_page_script":
        _ensure_server()
        code, data = _http("DELETE", f"/user-script/{urllib.parse.quote(args['id'])}")
        if code != 200:
            return {"ok": False, "error": data.get("detail")}
        return data
    if name == "web_journal":
        _ensure_server()
        qs = [f"limit={int(args.get('limit', 10))}"]
        if args.get("query"):
            qs.append("q=" + urllib.parse.quote(args["query"]))
        if args.get("host"):
            qs.append("host=" + urllib.parse.quote(args["host"]))
        if args.get("include_failed"):
            qs.append("all=true")
        return _http("GET", "/journal?" + "&".join(qs))[1]
    if name == "web_close_tab":
        _ensure_server()
        code, data = _http("POST", "/close", {"url": args.get("url"), "tab_id": args.get("tab_id")})
        if code != 200:
            return {"ok": False, "error": data.get("detail")}
        return data
    if name == "web_reload_extension":
        _ensure_server()
        return _http("POST", "/reload")[1]
    raise ValueError(f"unknown tool: {name}")


# --------------------------------------------------------------------------- #
# minimal MCP JSON-RPC over stdio (newline-delimited)
# --------------------------------------------------------------------------- #
def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def main():
    import urllib.parse  # noqa
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            _result(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "web-bridge", "version": "0.1.0"},
            })
        elif method == "notifications/initialized" or method == "initialized":
            pass  # notification, no reply
        elif method == "tools/list":
            _result(rid, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                out = call_tool(name, args)
                text = json.dumps(out, ensure_ascii=False, indent=2)
                _result(rid, {"content": [{"type": "text", "text": text}], "isError": not out.get("ok", True)})
            except Exception as e:  # noqa: BLE001
                _result(rid, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        elif method == "ping":
            _result(rid, {})
        elif rid is not None:
            _error(rid, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()

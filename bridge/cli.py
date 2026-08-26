#!/usr/bin/env python3
"""web-bridge CLI — drive logged-in browser pages from the shell.

Talks HTTP (with the shared token) to the local bridge server, which relays to
the browser extension. Auto-starts the server if it isn't running.

Examples
--------
  web-bridge status
  web-bridge tabs                       # list open tabs
  web-bridge tabs github                # filter
  web-bridge open https://example.com
  web-bridge exec 'return document.title' --url example.com
  web-bridge exec 'return __NEXT_DATA__.buildId' --site chatgpt   # MAIN-world: page globals!
  web-bridge exec 'return await fetch("/api/me").then(r=>r.json())' --site chatgpt
  web-bridge reload                     # reload the extension from disk
  web-bridge chatgpt "画一只赛博朋克猫" --images --out ~/Desktop
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import mimetypes
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
DEFAULT_OUT = os.path.join(HERE, "out")


def _http(method: str, path: str, body=None, timeout: float = 320):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
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
    # code 0 == the request never got an answer: the socket died, the server was
    # restarted mid-call, we timed out waiting. That is NOT the same as "the
    # work failed" — the browser may well have finished it — so it gets its own
    # code and the caller can go and claim the result instead of giving up.
    except urllib.error.URLError as e:
        return 0, {"detail": f"无法连接 bridge ({BASE}): {e.reason}", "transport": True}
    except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as e:
        return 0, {"detail": f"与 bridge 的连接中断: {type(e).__name__}: {e}", "transport": True}


def _reclaim(request_id: str, seconds: float):
    """Claim a result the server may still be holding for this request_id.

    Covers both halves of the drop: a connection that died while the command
    was still running (we long-poll until it finishes), and a server that was
    restarted and is coming back (we retry while it is unreachable).
    """
    end = time.time() + max(10.0, seconds)
    while time.time() < end:
        code, data = _http("GET", f"/result/{urllib.parse.quote(request_id)}?wait=20", timeout=40)
        if code == 200:
            if data.get("status") == "running":
                continue
            return data
        if code == 404:
            return None                      # server never knew, or already expired
        if code == 0:
            time.sleep(2)                    # server restarting — wait for it
            continue
        return None
    return None


def _call_recoverable(path: str, body: dict, timeout: float):
    """POST a page-driving command so that a lost connection cannot lose the work.

    The bug this exists for: three ChatGPT image generations, all three drawn on
    the page, all three thrown away because the HTTP connection broke on the way
    back. The command now carries a request_id; when the socket dies we ask the
    bridge for that id instead of reporting failure.
    """
    rid = body.get("request_id") or uuid.uuid4().hex
    body["request_id"] = rid
    code, data = _http("POST", path, body, timeout=timeout)
    if code != 0:
        return code, data, rid
    print(f"⚠️  {data.get('detail')}\n    页面上的活可能已经干完了——正在用 request_id {rid} 补捞…",
          file=sys.stderr)
    got = _reclaim(rid, timeout)
    if got is None:
        return 0, {"detail": f"连接中断，且服务端没有 request_id {rid} 的记录"
                             f"（命令多半在服务被重启时就断了）。\n"
                             f"    ChatGPT 的答案可以直接从页面补捞：wb chatgpt-last [--images --out DIR]",
                   "request_id": rid}, rid
    if got.get("status") == "done":
        print(f"✅ 已从服务端缓存补捞回结果（request_id {rid}）", file=sys.stderr)
        return 200, {"ok": True, **(got.get("result") or {})}, rid
    return got.get("code") or 502, {"detail": got.get("error") or "补捞到的是一条失败记录",
                                    "request_id": rid}, rid


def server_up() -> bool:
    code, _ = _http("GET", "/health", timeout=3)
    return code == 200


def ensure_server(autostart=True) -> bool:
    if server_up():
        return True
    if not autostart:
        return False
    # When the LaunchAgent is installed it owns the port; spawning a second
    # server here would just lose the race and exit, so ask launchd to start
    # the real one instead. Falls through to the ad-hoc spawn otherwise.
    if service.installed() and not service.IS_WINDOWS:
        subprocess.run(["launchctl", "kickstart", service.SERVICE],
                       capture_output=True, text=True)
        for _ in range(40):
            if server_up():
                return True
            time.sleep(0.2)
        print(f"[cli] 服务没起来，看日志：{service.ERR}", file=sys.stderr)
        return False
    log = open(os.path.join(HERE, "server.log"), "ab")
    try:
        subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=HERE)
    except Exception as e:  # noqa: BLE001
        print(f"[cli] 启动 bridge 失败: {e}", file=sys.stderr)
        return False
    for _ in range(40):
        if server_up():
            return True
        time.sleep(0.2)
    return False


def _need_server(args) -> bool:
    if not ensure_server(autostart=not getattr(args, "no_autostart", False)):
        print(json.dumps({"ok": False, "error": "bridge 服务未运行且无法启动"}, ensure_ascii=False))
        return False
    return True


def _emit(data, as_json):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        if isinstance(data, str):
            print(data)
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
def cmd_status(args):
    if not _need_server(args):
        return 2
    code, data = _http("GET", "/health")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        how = ("常驻服务 " + ("Startup 项" if service.IS_WINDOWS else "launchd") if service.installed() else "临时进程（wb service install 可装成常驻）")
        print(f"bridge: 运行中 ({BASE}) · {how}")
        print(f"扩展连接: {'✅' if data.get('extension_connected') else '❌ 未连接 (在 chrome://extensions 加载/重载 web-bridge)'}")
        print(f"已配置站点: {', '.join(data.get('sites') or []) or '(无)'}")
        b = data.get("build") or {}
        if b:
            print(f"代码: v{b.get('version')} sha {b.get('code_sha256')} · 启动于 {b.get('started_at')}")
            if b.get("stale"):
                # the "my fix isn't live" trap, stated instead of inferred
                print(f"⚠️  磁盘代码比这个进程新（{b.get('code_newest_file')} @ {b.get('code_mtime')}）"
                      f"——先 wb service restart，否则你排查的是旧代码")
        for v in data.get("inflight") or []:
            print(f"在跑: 目标 {v.get('target')} {v.get('action')}"
                  f"{'/' + v['method'] if v.get('method') else ''} 已 {v.get('seconds')}s")
    return 0 if data.get("extension_connected") else 1


def cmd_tabs(args):
    if not _need_server(args):
        return 2
    code, data = _http("GET", "/tabs" + (f"?filter={urllib.parse.quote(args.filter)}" if args.filter else ""))
    if code != 200:
        _emit(data, args.json); return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for t in data.get("tabs", []):
            print(f"[{t['id']}] {t.get('title','')[:50]:52} {t.get('url','')}")
    return 0


def cmd_open(args):
    if not _need_server(args):
        return 2
    code, data = _http("POST", "/open", {"url": args.url, "activate": not args.background, "reuse": not args.new})
    _emit(data if args.json else f"opened tab {data.get('tabId')} -> {data.get('url')}", args.json)
    return 0 if code == 200 else 1


def cmd_stats(args):
    """Answers 'are my saved tools being used, or is it rewriting JS every time'."""
    if not _need_server(args):
        return 2
    qs = f"?days={args.days}" + (f"&host={urllib.parse.quote(args.host)}" if args.host else "")
    code, d = _http("GET", "/journal/stats" + qs)
    if code != 200:
        _emit(d, args.json); return 1
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2)); return 0
    total = d["capability_runs"] + d["adhoc_execs"]
    print(f"最近 {d['days']} 天" + (f"（{args.host}）" if args.host else ""))
    print(f"  Agent Tools 调用   {d['capability_runs']:>5}")
    print(f"  临时现写 JS        {d['adhoc_execs']:>5}")
    print(f"  Page Tools 运行    {d['user_script_runs']:>5}")
    if total:
        print(f"  复用率             {d['reuse_rate']*100:>4.0f}%   "
              f"（现写占 {100 - d['reuse_rate']*100:.0f}%——比例越低说明存下来的工具越没被用上）")
    if d["tools"]:
        print("\n  用过的 Agent Tools：")
        for t in d["tools"]:
            print(f"    {t['id']:<34} {t['runs']} 次 · 平均 {t['avg_ms']}ms"
                  + ("" if t["ok"] == t["runs"] else f" · 失败 {t['runs']-t['ok']} 次"))
    else:
        print("\n  这段时间没有任何 Agent Tool 被调用过。")
    if d.get("discoveries") is not None:
        print(f"  问过「这页有什么能力」  {d['discoveries']:>3} 次")
    if d["hosts"]:
        print("\n  按站点（能力 / 现写 / 页面脚本）：")
        for h in d["hosts"][:6]:
            print(f"    {h['host']:<28} {h['capability']:>3} / {h['exec']:>3} / {h['user-script']:>3}")
    # The two failure modes need opposite fixes, so never show them as one number
    gaps = d.get("gaps") or []
    missing = [g for g in gaps if not g["has_site_tool"]]
    unused = [g for g in gaps if g["has_site_tool"] and g["capability_runs"] == 0]
    if missing:
        print("\n  ⚠️  这些站点一直在现写 JS，但没有任何 Agent Tool（缺工具）：")
        for g in missing:
            print(f"    {g['host']:<28} 现写 {g['adhoc']} 次 — 值得沉淀一个能力")
    if unused:
        print("\n  ⚠️  这些站点有工具却没被调用（工具没命中）：")
        for g in unused:
            print(f"    {g['host']:<28} 现写 {g['adhoc']} 次，能力 0 次")
    return 0


def cmd_log(args):
    """Show what has already been run — the 'look before you write JS' command."""
    if not _need_server(args):
        return 2
    qs = []
    if args.grep:
        qs.append("q=" + urllib.parse.quote(args.grep))
    if args.host:
        qs.append("host=" + urllib.parse.quote(args.host))
    qs.append(f"limit={args.lines}")
    if args.all:
        qs.append("all=true")
    code, data = _http("GET", "/journal?" + "&".join(qs))
    if code != 200:
        _emit(data, args.json); return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2)); return 0
    st = data.get("stats") or {}
    print(f"日志 {st.get('log')}  ({st.get('exec_runs',0)} 次 exec / "
          f"{st.get('distinct_scripts',0)} 段不同脚本 / {st.get('capability_runs',0)} 次能力调用，"
          f"跑满 {st.get('promote_after')} 次自动沉淀)")
    matches = data.get("matches") or []
    if not matches:
        print("没有匹配的记录"); return 0
    for m in matches:
        tag = f" → 已沉淀 {m['promoted_to']}" if m.get("promoted_to") else ""
        name = m.get("capability") or m.get("summary") or "(无描述)"
        print(f"\n[{m['ok_runs']}/{m['runs']} 次成功] {m['host']}  {name}{tag}")
        print(f"  最近 {m['last']}  签名 {m['signature']}")
        if args.code and m.get("code"):
            print("  ---")
            for line in m["code"].splitlines()[: args.code_lines]:
                print("  " + line)
    return 0


def cmd_agents(args):
    """Show / re-detect the local agent CLIs the side panel's chat can call."""
    if not _need_server(args):
        return 2
    if args.detect:
        code, data = _http("POST", "/agents/detect",
                           {"cwd": args.cwd, "full_access": not args.no_full_access})
    else:
        code, data = _http("GET", "/agents")
    if code != 200:
        _emit(data, args.json); return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2)); return 0
    runners = data.get("runners") or {}
    if not runners:
        print("没有探测到本地 agent（claude / codex / dsh）。装好之后跑 wb agents --detect")
        return 1
    for name, r in runners.items():
        mark = "✅" if r.get("available") else "❌"
        star = "  ←默认" if name == data.get("default") else ""
        print(f"{mark} {name}{star}  {r.get('label','')}")
        print(f"     {r.get('path')} {' '.join(r.get('args') or [])}")
        print(f"     工作目录 {r.get('cwd')}")
    return 0


def cmd_service(args):
    return {
        "install": service.cmd_install, "uninstall": service.cmd_uninstall,
        "restart": service.cmd_restart, "status": service.cmd_status,
        "logs": service.cmd_logs,
    }[args.action](args)


def cmd_close(args):
    if not _need_server(args):
        return 2
    body = {"tab_id": int(args.target)} if args.target.isdigit() else {"url": args.target}
    code, data = _http("POST", "/close", body)
    if code != 200:
        _emit(data, args.json); return 1
    closed = data.get("closed") or []
    if args.json:
        _emit(data, True)
    elif not closed:
        print("没有匹配的标签页")
    else:
        for t in closed:
            print(f"已关闭 [{t['id']}] {t.get('title','')[:40]}  {t.get('url','')[:60]}")
    return 0


def cmd_exec(args):
    if not _need_server(args):
        return 2
    parsed = None
    if args.args:
        parsed = json.loads(args.args)
    body = {"code": args.code, "args": parsed, "site": args.site, "url": args.url,
            "new_tab": args.new_tab, "timeout_ms": int(args.timeout * 1000)}
    code, data, _rid = _call_recoverable("/exec", body, timeout=args.timeout + 15)
    if code != 200:
        err = data.get("detail") or data.get("error") or f"HTTP {code}"
        _emit({"ok": False, "error": err} if args.json else f"❌ {err}", args.json)
        return 1
    _emit(data.get("result") if not args.json else data, args.json)
    hint = (data.get("journal") or {}).get("hint")
    if hint and not args.json:
        print(f"\n💡 {hint}", file=sys.stderr)
    return 0


def cmd_reload(args):
    if not _need_server(args):
        return 2
    code, data = _http("POST", "/reload")
    _emit(data if args.json else ("♻️  扩展重载中…" if code == 200 else f"❌ {data.get('detail')}"), args.json)
    return 0 if code == 200 else 1


def _encode_file(path):
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise SystemExit(f"文件不存在: {path}")
    mime, _ = mimetypes.guess_type(path)
    with open(path, "rb") as f:
        return {"name": os.path.basename(path), "mime": mime or "application/octet-stream",
                "b64": base64.b64encode(f.read()).decode()}


def _save_images(images, out_dir):
    if not images:
        return []
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths = []
    for i, im in enumerate(images, 1):
        ext = (im.get("mime", "image/png").split("/")[-1].split(";")[0]) or "png"
        if ext == "jpeg":
            ext = "jpg"
        p = os.path.join(out_dir, f"wb-{stamp}-{i}.{ext}")
        with open(p, "wb") as f:
            f.write(base64.b64decode(im["b64"]))
        paths.append(p)
    return paths


def _fmt_spec(name, spec):
    spec = spec or {}
    bits = [spec.get("type", "any")]
    if spec.get("required"):
        bits.append("必填")
    if "default" in spec:
        bits.append("默认 " + json.dumps(spec["default"], ensure_ascii=False))
    if spec.get("enum"):
        bits.append("/".join(str(x) for x in spec["enum"]))
    line = f"  {name} ({', '.join(bits)})"
    return line + (f" — {spec['description']}" if spec.get("description") else "")


def cmd_caps(args):
    if not _need_server(args):
        return 2
    if getattr(args, "capability", None):
        # detail view: everything needed to call it right, or to edit it
        code, data = _http("GET", f"/capability/{urllib.parse.quote(args.capability)}")
        if code != 200:
            _emit(data, args.json); return 1
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2)); return 0
        c = data["capability"]
        scope = "通用" if c.get("match") == ["*"] else ",".join(c.get("match", []))
        print(f"{c['id']}  —  {c.get('title','')}   [{c.get('kind','?')} · {scope}]")
        if c.get("description"):
            print(f"\n{c['description']}")
        print("\n参数:")
        print("\n".join(_fmt_spec(n, sp) for n, sp in (c.get("params") or {}).items()) or "  （无）")
        if args.source:
            print("\n--- 源码 ---\n" + data.get("source", ""))
        else:
            print(f"\n源码: wb caps {c['id']} --source     文件: {c.get('file','')}")
        return 0
    qs = ""
    if args.url:
        qs = f"?url={urllib.parse.quote(args.url)}"
    elif args.site:
        qs = f"?site={urllib.parse.quote(args.site)}"
    code, data = _http("GET", "/capabilities" + qs)
    if code != 200:
        _emit(data, args.json); return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for c in data.get("capabilities", []):
            scope = "通用" if c.get("match") == ["*"] else ",".join(c.get("match", []))
            print(f"[{c.get('kind','?'):8}] {c['id']:20} {c.get('title','')}  ({scope})")
            if c.get("description"):
                print(f"           {c['description'][:100]}")
            if c.get("params"):
                names = [n + ("*" if (sp or {}).get("required") else "")
                         for n, sp in c["params"].items()]
                print(f"           参数: {', '.join(names)}   (* = 必填)"
                      if any(n.endswith("*") for n in names) else f"           参数: {', '.join(names)}")
    return 0


def cmd_run(args):
    if not _need_server(args):
        return 2
    params = json.loads(args.params) if args.params else {}
    body = {"params": params, "site": args.site, "url": args.url,
            "new_tab": args.new_tab, "timeout_ms": int(args.timeout * 1000)}
    code, data, _rid = _call_recoverable(f"/capability/{args.capability}", body,
                                         timeout=args.timeout + 15)
    if code != 200:
        err = data.get("detail") or data.get("error") or f"HTTP {code}"
        _emit({"ok": False, "error": err} if args.json else f"❌ {err}", args.json)
        return 1
    _emit(data.get("result") if not args.json else data, args.json)
    return 0


def cmd_save_cap(args):
    if not _need_server(args):
        return 2
    src = open(os.path.expanduser(args.file), encoding="utf-8").read()
    code, data = _http("PUT", f"/capability/{args.capability}", {"source": src})
    _emit(data, args.json)
    return 0 if code == 200 else 1


def cmd_sites(args):
    if not _need_server(args):
        return 2
    if args.add:
        body = {"match": args.match or [args.add], "home": args.home, "adapter": args.adapter}
        code, data = _http("PUT", f"/site/{args.add}", body)
    elif args.remove:
        code, data = _http("DELETE", f"/site/{args.remove}")
    else:
        code, data = _http("GET", "/sites")
    if args.json or args.add or args.remove:
        _emit(data, True); return 0 if code == 200 else 1
    for name, s in (data.get("sites") or {}).items():
        print(f"{name:14} match={','.join(s.get('match', []))}"
              + (f"  home={s['home']}" if s.get("home") else "")
              + (f"  adapter={s['adapter']}" if s.get("adapter") else ""))
    return 0


def cmd_adapter(args):
    if not _need_server(args):
        return 2
    params = json.loads(args.params) if args.params else {}
    code, data, _rid = _call_recoverable(
        f"/adapter/{args.site}/{args.method}",
        {"params": params, "timeout_ms": int(args.timeout * 1000)}, timeout=args.timeout + 15)
    if code != 200:
        _emit(data, args.json); return 1
    _emit(data.get("result") if not args.json else data, args.json)
    return 0


def cmd_chatgpt(args):
    if not _need_server(args):
        return 2
    params = {"prompt": args.prompt, "new_chat": args.new, "want_images": args.images,
              "files": [_encode_file(p) for p in (args.file or [])],
              # leave the bridge a margin so the page reports its own error
              # instead of both sides timing out at the same instant
              "deadline_ms": int(max(30, args.timeout - 25) * 1000)}
    code, data, rid = _call_recoverable(
        "/adapter/chatgpt/ask",
        {"params": params, "timeout_ms": int(args.timeout * 1000)}, timeout=args.timeout + 15)
    if code != 200 or not data.get("ok"):
        # Last line of defence: the answer exists on the page even when neither
        # the socket nor the server-side cache could deliver it. One read of the
        # conversation API — no re-prompt, no extra quota spent.
        if code == 0 and not args.no_recover:
            print("↩️  服务端也没有缓存，改从页面直接补捞最后一条回答…", file=sys.stderr)
            got = _chatgpt_last(want_images=args.images, timeout=120)
            if got is not None:
                data, code = {"ok": True, "result": got}, 200
        if code != 200 or not data.get("ok"):
            err = data.get("detail") or data.get("error") or f"HTTP {code}"
            _emit({"ok": False, "error": err, "request_id": rid} if args.json else f"❌ {err}", args.json)
            return 1
    res = data.get("result", data)
    saved = _save_images(res.get("images") or [], os.path.expanduser(args.out))
    if args.json:
        print(json.dumps({"ok": True, "text": res.get("text"), "images": saved,
                          "conversation_url": res.get("conversation_url"),
                          "request_id": rid}, ensure_ascii=False, indent=2))
    else:
        if res.get("text"):
            print(res["text"])
        for p in saved:
            print(f"\n[图片已保存] {p}")
    return 0


def _chatgpt_last(want_images: bool, timeout: float, conversation: str = ""):
    """Read a ChatGPT conversation's last answer back off the page.

    With no conversation id it reads the one open in web-bridge's own tab; with
    one it reads that conversation instead (same origin, same cookies), which is
    how you go back for an answer whose tab has since moved on.
    """
    params = {"want_images": want_images}
    if conversation:
        params["conversation_id"] = conversation
    code, data, _rid = _call_recoverable(
        "/adapter/chatgpt/last",
        {"params": params, "timeout_ms": int(timeout * 1000)}, timeout=timeout + 15)
    if code != 200 or not data.get("ok"):
        return None
    return data.get("result")


def cmd_chatgpt_last(args):
    """Fetch the last answer of the ChatGPT conversation already on screen.

    Sends nothing. This is the "the transport died but ChatGPT already did the
    work" recovery: it walks the conversation tree from `current_node` up the
    `parent` chain (create_time ordering lies) and pulls the generated images
    out of the `role:"tool"` messages, downloading their bytes inside the page
    because the signed URLs are cookie-gated.
    """
    if not _need_server(args):
        return 2
    res = _chatgpt_last(want_images=args.images, timeout=args.timeout,
                        conversation=args.conversation or "")
    if res is None:
        _emit({"ok": False, "error": "补捞失败"} if args.json else "❌ 补捞失败（见上面的错误）", args.json)
        return 1
    saved = _save_images(res.get("images") or [], os.path.expanduser(args.out))
    if args.json:
        print(json.dumps({**res, "images": saved}, ensure_ascii=False, indent=2))
    else:
        if res.get("text"):
            print(res["text"])
        for p in saved:
            print(f"\n[图片已保存] {p}")
        if not res.get("text") and not saved:
            print("（这个会话的最后一轮既没有文字也没有图片）")
    return 0


def cmd_result(args):
    """Claim (or inspect) the result of an earlier request by its request_id."""
    if not _need_server(args):
        return 2
    code, data = _http("GET", f"/result/{urllib.parse.quote(args.request_id)}"
                              f"?wait={args.wait}", timeout=args.wait + 20)
    if code != 200:
        _emit(data, args.json); return 1
    res = (data.get("result") or {}).get("result") if isinstance(data.get("result"), dict) else None
    if args.json or not res:
        print(json.dumps(data, ensure_ascii=False, indent=2)); return 0
    if isinstance(res, dict) and res.get("images"):
        for p in _save_images(res["images"], os.path.expanduser(args.out)):
            print(f"[图片已保存] {p}")
    if isinstance(res, dict) and res.get("text"):
        print(res["text"])
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_results(args):
    if not _need_server(args):
        return 2
    code, data = _http("GET", f"/results?limit={args.lines}")
    if code != 200:
        _emit(data, args.json); return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2)); return 0
    rows = data.get("results") or []
    if not rows:
        print("没有可补捞的结果（只有带 request_id 的调用会留存）"); return 0
    for r in rows:
        meta = r.get("meta") or {}
        bits = [meta.get("capability") or meta.get("kind") or "?"]
        if meta.get("site"):
            bits.append(meta["site"])
        if meta.get("method"):
            bits.append(meta["method"])
        what = " ".join(bits)
        print(f"{r['request_id']}  {r['status']:8} {what:28} {r['age_seconds']}s 前"
              + (f"  ❗{r['error'][:60]}" if r.get("error") else ""))
    print("\n领取：wb result <request_id> [--out DIR]")
    return 0


# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(prog="web-bridge", description="Drive logged-in browser pages from the CLI.")
    p.add_argument("--no-autostart", action="store_true")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status"); s.set_defaults(func=cmd_status)
    t = sub.add_parser("tabs"); t.add_argument("filter", nargs="?", default=""); t.set_defaults(func=cmd_tabs)
    o = sub.add_parser("open"); o.add_argument("url"); o.add_argument("--background", action="store_true"); o.add_argument("--new", action="store_true"); o.set_defaults(func=cmd_open)

    e = sub.add_parser("exec", help="在页面 MAIN world 执行 JS（函数体，args 在作用域，可 return/await）")
    e.add_argument("code"); e.add_argument("--site"); e.add_argument("--url")
    e.add_argument("--args", help="JSON，作为 args 传入")
    e.add_argument("--new-tab", action="store_true"); e.add_argument("--timeout", type=float, default=30)
    e.set_defaults(func=cmd_exec)

    cl = sub.add_parser("close", help="关闭标签页（按 URL 片段或 tab id）")
    cl.add_argument("target", help="URL 片段或 tab id"); cl.set_defaults(func=cmd_close)
    st2 = sub.add_parser("stats", help="工具复用率：存下来的能力到底有没有被用上")
    st2.add_argument("--days", type=int, default=7)
    st2.add_argument("--host", default="", help="只看某个站点")
    st2.set_defaults(func=cmd_stats)

    lg2 = sub.add_parser("log", help="查以前在这个站跑过什么（写新脚本前先看这里）")
    lg2.add_argument("grep", nargs="?", default="", help="关键词（匹配说明和代码）")
    lg2.add_argument("--host", default="", help="只看某个站点")
    lg2.add_argument("-n", "--lines", type=int, default=10, help="最多列几条")
    lg2.add_argument("--code", action="store_true", help="连代码一起打印")
    lg2.add_argument("--code-lines", type=int, default=20, help="每条打印多少行代码")
    lg2.add_argument("--all", action="store_true", help="连失败过的也列出来")
    lg2.set_defaults(func=cmd_log)

    ag = sub.add_parser("agents", help="侧栏对话可调用的本地 agent（claude/codex/dsh）")
    ag.add_argument("--detect", action="store_true", help="重新探测并写入配置")
    ag.add_argument("--cwd", help="agent 的默认工作目录")
    ag.add_argument("--no-full-access", action="store_true", help="不加跳过确认的参数")
    ag.set_defaults(func=cmd_agents)

    sv = sub.add_parser("service", help="装成开机自启的后台服务（macOS launchd / Windows Startup 项）")
    sv.add_argument("action", choices=["install", "uninstall", "restart", "status", "logs"])
    sv.add_argument("-n", "--lines", type=int, default=40, help="logs 显示多少行")
    sv.add_argument("--force", action="store_true",
                    help="即使服务上有命令在跑也重启/卸载（会打断它们）")
    sv.set_defaults(func=cmd_service)
    r = sub.add_parser("reload", help="让扩展从磁盘重载"); r.set_defaults(func=cmd_reload)

    cp = sub.add_parser("caps", help="列出当前页面可用的能力；带 id 则看单个能力的参数与源码")
    cp.add_argument("capability", nargs="?", help="能力 id（不填则列出全部）")
    cp.add_argument("--url"); cp.add_argument("--site")
    cp.add_argument("--source", action="store_true", help="连同源码一起打印")
    cp.set_defaults(func=cmd_caps)

    rn = sub.add_parser("run", help="运行一个能力")
    rn.add_argument("capability"); rn.add_argument("--params", help="JSON 参数")
    rn.add_argument("--site"); rn.add_argument("--url"); rn.add_argument("--new-tab", action="store_true")
    rn.add_argument("--timeout", type=float, default=120); rn.set_defaults(func=cmd_run)

    st = sub.add_parser("sites", help="查看/注册站点（无需改 manifest 或重载扩展）")
    st.add_argument("--add", help="站点名"); st.add_argument("--match", action="append", help="URL 匹配（可多次）")
    st.add_argument("--home", help="没有已开标签时打开的地址"); st.add_argument("--adapter", help="适配器名")
    st.add_argument("--remove", help="删除站点名"); st.set_defaults(func=cmd_sites)

    sc = sub.add_parser("save-cap", help="保存/更新一个能力文件")
    sc.add_argument("capability"); sc.add_argument("file"); sc.set_defaults(func=cmd_save_cap)

    a = sub.add_parser("adapter", help="调用站点适配器方法")
    a.add_argument("site"); a.add_argument("method"); a.add_argument("--params", help="JSON")
    a.add_argument("--timeout", type=float, default=300); a.set_defaults(func=cmd_adapter)

    c = sub.add_parser("chatgpt", help="向已登录 ChatGPT 提问（文本/文件/出图）")
    c.add_argument("prompt"); c.add_argument("--new", action="store_true")
    c.add_argument("--file", action="append"); c.add_argument("--images", action="store_true")
    c.add_argument("--out", default=DEFAULT_OUT); c.add_argument("--timeout", type=float, default=300)
    c.add_argument("--no-recover", action="store_true",
                   help="连接断了就直接失败，不做补捞（默认会补捞）")
    c.set_defaults(func=cmd_chatgpt)

    cll = sub.add_parser("chatgpt-last",
                         help="不发消息，把 ChatGPT 当前会话的最后一条回答（含图片）取回来")
    cll.add_argument("--images", action="store_true", help="连生成的图片一起下载")
    cll.add_argument("--conversation", help="指定会话 id（chatgpt.com/c/<id> 里的那段）；"
                                            "不填就用 web-bridge 自己那个标签页当前的会话")
    cll.add_argument("--out", default=DEFAULT_OUT); cll.add_argument("--timeout", type=float, default=120)
    cll.set_defaults(func=cmd_chatgpt_last)

    rs = sub.add_parser("result", help="按 request_id 领取之前那次调用的结果（连接断了用这个）")
    rs.add_argument("request_id")
    rs.add_argument("--wait", type=float, default=0, help="还在跑就等这么多秒")
    rs.add_argument("--out", default=DEFAULT_OUT, help="有图片时保存到哪")
    rs.set_defaults(func=cmd_result)

    rss = sub.add_parser("results", help="列出还能补捞的 request_id")
    rss.add_argument("-n", "--lines", type=int, default=20)
    rss.set_defaults(func=cmd_results)
    return p


def main():
    import urllib.parse  # noqa
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    import urllib.parse  # noqa
    sys.exit(main())

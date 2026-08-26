#!/usr/bin/env python3
"""Run the bridge as a macOS LaunchAgent: starts at login, restarts if it dies.

Without this, the server only exists because some CLI call happened to spawn it,
which means the extension shows "disconnected" until someone runs a command, and
a crash stays a crash until noticed. As a service it is simply always there.

    python3 bridge/service.py install     # write the plist + take over :8790
    python3 bridge/service.py status
    python3 bridge/service.py restart | logs | uninstall

Ownership matters more than it looks: two servers fighting over port 8790 is the
project's oldest failure mode (the loser exits silently and it reads as "the
extension disconnected"). So install kills whatever holds the port first, and the
server itself steps aside if a healthy web-bridge already owns it.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402
import agents  # noqa: E402

LABEL = "com.web-bridge.server"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library/Logs/web-bridge.log"
ERR = Path.home() / "Library/Logs/web-bridge.err.log"
DOMAIN = f"gui/{os.getuid()}"
SERVICE = f"{DOMAIN}/{LABEL}"


def _run(*args: str) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def _python() -> str:
    """The interpreter that actually has fastapi/uvicorn installed.

    Checked by actually importing them, not by guessing from the path.
    """
    # /usr/bin/python3 first on purpose: it is a stable system path that keeps
    # working across Xcode/CLT changes, whereas sys.executable may point deep
    # inside Xcode.app — a plist that outlives an Xcode move is worth more here.
    for candidate in ("/usr/bin/python3", sys.executable, "/opt/homebrew/bin/python3"):
        if not candidate or not Path(candidate).exists():
            continue
        code, _ = _run(candidate, "-c", "import fastapi, uvicorn, websockets")
        if code == 0:
            return candidate
    raise SystemExit("找不到装了 fastapi/uvicorn 的 python3——先 pip3 install -r bridge/requirements.txt")


def plist_body() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [_python(), str(HERE / "server.py")],
        "WorkingDirectory": str(HERE),
        "RunAtLoad": True,
        # Restart when it dies or exits non-zero, but NOT on a clean exit: the
        # server exits 0 on purpose when another web-bridge already owns the
        # port, and restarting that would be an infinite loop.
        "KeepAlive": {"Crashed": True, "SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(ERR),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def installed() -> bool:
    return PLIST.is_file()


def loaded() -> bool:
    return _run("launchctl", "print", SERVICE)[0] == 0


def health(timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(config.base_url() + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def wait_healthy(seconds: float = 12.0, want_extension: bool = False) -> dict | None:
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        h = health()
        if h:
            last = h
            if not want_extension or h.get("extension_connected"):
                return h
        time.sleep(0.4)
    return last


def port_owner_pids() -> list[int]:
    """Only the LISTENING process. A bare `lsof -ti :8790` also lists every
    client holding a connection — including Chrome, whose extension keeps the
    WebSocket open. Killing that list would kill the user's browser."""
    code, out = _run("lsof", "-ti", f"tcp:{config.PORT}", "-sTCP:LISTEN")
    return [int(x) for x in out.split() if x.isdigit()] if code == 0 else []


def free_port() -> None:
    """Take the port. `pkill -f "python3 server.py"` does NOT match the real
    command line — killing by port is the only reliable way (learned the hard
    way; see HANDOFF)."""
    pids = port_owner_pids()
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    time.sleep(0.8)


MAX_LOG_BYTES = 5 * 1024 * 1024


def rotate_logs() -> None:
    """Keep one previous generation. launchd opens the log at process start, so
    rotating here (rather than while it runs) is the safe moment."""
    for path in (LOG, ERR):
        try:
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                path.replace(path.with_suffix(path.suffix + ".1"))
        except OSError:
            pass


def inflight() -> list[dict]:
    """Commands the live server is running right now (empty if it is down)."""
    h = health() or {}
    return h.get("inflight") or []


def guard_inflight(args, what: str) -> bool:
    """Refuse to yank the server out from under a running command.

    This is the second half of the "lost result" bug. `launchctl kickstart -k`
    sends SIGTERM, and a `wb chatgpt --images` that is four minutes into
    generating pictures dies with it — the caller sees only
    `RemoteDisconnected`, there is no traceback anywhere (nothing crashed), and
    the account quota is spent for nothing. Restarting after an edit is a normal
    thing to do; doing it blind is what hurts. So: say what is running, and make
    the caller opt in with --force.
    """
    if getattr(args, "force", False):
        return True
    running = inflight()
    live = live_agent_runs()
    if not running and not live:
        return True
    print(f"⛔ 拒绝{what}：服务上还有活在跑，现在重启会打断它们：", file=sys.stderr)
    for v in running:
        print(f"   · 页面命令 目标 {v.get('target')}  {v.get('action')}"
              f"{'/' + v['method'] if v.get('method') else ''}  已 {v.get('seconds')}s", file=sys.stderr)
    for rid in live:
        # an agent run is minutes of work and real quota; killing one silently
        # is what made a user's task simply produce nothing
        print(f"   · 侧栏 agent 任务 {rid}（重启会杀掉它，已产生的输出会留在历史里）", file=sys.stderr)
    print("   等它跑完，或确认要打断就加 --force。", file=sys.stderr)
    return False


def cmd_install(args) -> int:
    if not guard_inflight(args, "安装/接管端口"):
        return 2
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)

    # Probe for local agent CLIs now and write them into config.json, so the side
    # panel's chat tab has a roster without anyone hand-writing argv.
    block = agents.detect(getattr(args, "agent_cwd", None) or None,
                          not getattr(args, "no_full_access", False))
    agents.save(block)
    if block["runners"]:
        names = ", ".join(block["runners"])
        cwd = block["runners"][block["default"]]["cwd"]
        print(f"  本地 agent：{names}（默认 {block['default']}，工作目录 {cwd}）")
    else:
        print("  本地 agent：未探测到 claude / codex / dsh —— 侧栏对话标签会显示不可用")
    body = plist_body()
    PLIST.write_bytes(plistlib.dumps(body))
    print(f"写入 {PLIST}")
    print(f"  解释器 {body['ProgramArguments'][0]}")

    if loaded():
        _run("launchctl", "bootout", SERVICE)
    rotate_logs()
    free_port()                                   # the service must own the port
    code, out = _run("launchctl", "bootstrap", DOMAIN, str(PLIST))
    if code != 0 and "already bootstrapped" not in out.lower():
        # older syntax as a fallback
        code, out = _run("launchctl", "load", "-w", str(PLIST))
        if code != 0:
            print(f"launchctl 加载失败：{out}", file=sys.stderr)
            return 1

    h = wait_healthy(want_extension=True)         # give the extension a moment to reconnect
    if not h:
        print(f"服务已注册但没起来，看日志：{ERR}", file=sys.stderr)
        return 1
    print(f"✅ 服务已启动（开机自启，崩溃自动重启）")
    print(f"   {config.base_url()}  扩展连接: {'✅' if h.get('extension_connected') else '❌ 等扩展重连'}")
    print(f"   日志 {LOG}")
    return 0


def cmd_uninstall(args) -> int:
    if not guard_inflight(args, "卸载"):
        return 2
    if loaded():
        _run("launchctl", "bootout", SERVICE)
    _run("launchctl", "unload", "-w", str(PLIST))
    if PLIST.exists():
        PLIST.unlink()
        print(f"已删除 {PLIST}")
    free_port()
    print("服务已卸载（wb 命令仍会按需临时拉起 server）")
    return 0


def live_agent_runs() -> list:
    h = health()
    return (h or {}).get("live_agent_runs") or []


def cmd_restart(args) -> int:
    if not installed():
        print("服务没装，先 install", file=sys.stderr)
        return 1
    if not guard_inflight(args, "重启"):
        return 2
    rotate_logs()
    code, out = _run("launchctl", "kickstart", "-k", SERVICE)
    if code != 0:
        print(f"重启失败：{out}", file=sys.stderr)
        return 1
    h = wait_healthy(want_extension=True)
    if h:
        print(f"✅ 已重启  扩展连接: {'✅' if h.get('extension_connected') else '❌ 等扩展重连'}")
    else:
        print(f"重启了但没起来，看日志：{ERR}", file=sys.stderr)
    return 0 if h else 1


def cmd_status(args) -> int:
    h = health()
    pids = port_owner_pids()
    print(f"plist:      {'已安装 ' + str(PLIST) if installed() else '未安装'}")
    print(f"launchd:    {'已加载' if loaded() else '未加载'}")
    if loaded():
        code, out = _run("launchctl", "print", SERVICE)
        for line in out.splitlines():
            k = line.strip()
            if k.startswith(("state =", "pid =", "last exit code =", "runs =")):
                print(f"            {k}")
    print(f"端口 {config.PORT}:  {'占用 pid ' + ','.join(map(str, pids)) if pids else '空闲'}")
    if h:
        print(f"服务:       ✅ 运行中  扩展连接: {'✅' if h.get('extension_connected') else '❌'}")
        b = h.get("build") or {}
        if b:
            # The trap this prints for: the fix is on disk, the daemon is the
            # process from before the fix, and the live traceback points at a
            # line number that no longer exists.
            print(f"代码:       v{b.get('version')} sha {b.get('code_sha256')}  "
                  f"启动于 {b.get('started_at')}（{b.get('uptime_seconds')}s）")
            if b.get("stale"):
                print(f"            ⚠️  磁盘上的代码更新过（{b.get('code_newest_file')} @ "
                      f"{b.get('code_mtime')}）——跑的是旧进程，改动没生效，需要 restart")
        for v in h.get("inflight") or []:
            print(f"在跑:       目标 {v.get('target')}  {v.get('action')}"
                  f"{'/' + v['method'] if v.get('method') else ''}  已 {v.get('seconds')}s")
    else:
        print("服务:       ❌ 没响应")
    return 0 if h else 1


def cmd_logs(args) -> int:
    for path in (LOG, ERR):
        if not path.exists():
            continue
        print(f"=== {path} ===")
        code, out = _run("tail", "-n", str(args.lines), str(path))
        print(out or "(空)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("install", cmd_install), ("uninstall", cmd_uninstall), ("restart", cmd_restart)):
        sp = sub.add_parser(name)
        sp.add_argument("--force", action="store_true",
                        help="即使有命令在跑也照做（会打断它们，结果可能丢）")
        if name == "install":
            sp.add_argument("--agent-cwd", help="侧栏 agent 的默认工作目录（默认 ~/cc）")
            sp.add_argument("--no-full-access", action="store_true",
                            help="不给 agent 加跳过确认的参数（默认加，见 README 安全说明）")
        sp.set_defaults(func=fn)
    sub.add_parser("status").set_defaults(func=cmd_status)
    lg = sub.add_parser("logs"); lg.add_argument("-n", "--lines", type=int, default=40)
    lg.set_defaults(func=cmd_logs)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())

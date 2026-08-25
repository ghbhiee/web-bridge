"""Local agent runners — let the side panel talk to claude / codex / dsh.

The side panel's chat tab does not contain an agent; it *drives* the ones already
installed on this machine, in their headless modes:

    claude -p --output-format stream-json …
    codex exec --json …
    dsh --profile headless …

Detection happens once (at service install, or `wb agents --detect`) and lands in
~/.config/web-bridge/config.json, so the extension can read the roster through
the bridge instead of guessing what is installed.

Each runner declares how to build its argv and how to turn its stdout into plain
text events, because the three CLIs stream in three different shapes:
claude emits stream-json objects, codex emits JSONL events, dsh emits raw text.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import config

# --------------------------------------------------------------------------- #
# the roster
# --------------------------------------------------------------------------- #
# `args` is the headless invocation; the prompt is appended last (or piped).
# `full_access` is what the user opted into when installing: these CLIs run with
# their own permission model otherwise, which would hang a non-interactive run on
# an approval prompt nobody can answer.
KNOWN = {
    "claude": {
        "label": "Claude Code",
        "bin": "claude",
        "args": ["-p", "--output-format", "stream-json", "--verbose"],
        "full_access_args": ["--dangerously-skip-permissions"],
        "format": "claude-stream-json",
        "resume_args": ["--resume"],
    },
    "codex": {
        "label": "Codex",
        "bin": "codex",
        "args": ["exec", "--json"],
        "full_access_args": ["--dangerously-bypass-approvals-and-sandbox"],
        "format": "codex-jsonl",
        "resume_args": None,          # codex resumes by subcommand, not a flag
    },
    "dsh": {
        "label": "DeepSeek Harness",
        "bin": "dsh",
        "args": ["--profile", "headless"],
        "full_access_args": [],
        "format": "text",
        "resume_args": None,
    },
}

DEFAULT_CWD = str(Path.home() / "cc")


def detect(cwd: Optional[str] = None, full_access: bool = True) -> dict:
    """Find which agent CLIs exist and build a ready-to-use config block.

    Called at service install so the panel has a roster without the user
    hand-writing argv. Re-run any time with `wb agents --detect`.
    """
    runners: dict[str, dict] = {}
    for name, spec in KNOWN.items():
        path = shutil.which(spec["bin"])
        if not path:
            continue
        args = list(spec["args"])
        if full_access:
            args += spec["full_access_args"]
        runners[name] = {
            "label": spec["label"],
            "path": path,
            "args": args,
            "cwd": cwd or DEFAULT_CWD,
            "format": spec["format"],
            "enabled": True,
        }
    order = [n for n in ("claude", "codex", "dsh") if n in runners]
    return {"default": order[0] if order else "", "runners": runners}


def roster() -> dict:
    """The configured agents, refreshed against reality.

    A binary can disappear (uninstalled, PATH change) long after detection, and
    the panel showing a dead agent is worse than showing none — so availability
    is checked at read time, not trusted from the file.
    """
    block = config.CFG.get("agents") or {}
    runners = dict(block.get("runners") or {})
    for name, r in runners.items():
        path = r.get("path") or shutil.which(KNOWN.get(name, {}).get("bin", name) or name)
        r["available"] = bool(path and Path(path).exists())
    default = block.get("default") or ""
    if default not in runners or not runners.get(default, {}).get("available"):
        alive = [n for n, r in runners.items() if r.get("available") and r.get("enabled", True)]
        default = alive[0] if alive else ""
    return {"default": default, "runners": runners, "cwd_default": DEFAULT_CWD}


def save(block: dict) -> None:
    """Persist the agents block back to config.json (chmod 600)."""
    data = {}
    if config.CONFIG_PATH.is_file():
        try:
            data = json.loads(config.CONFIG_PATH.read_text())
        except Exception:  # noqa: BLE001
            data = {}
    data["agents"] = block
    config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    config.CONFIG_PATH.chmod(0o600)
    config.CFG["agents"] = block


# --------------------------------------------------------------------------- #
# output parsing — three CLIs, three shapes, one event stream
# --------------------------------------------------------------------------- #
def _parse_claude(line: str) -> list[dict]:
    """claude -p --output-format stream-json: one JSON object per line."""
    try:
        msg = json.loads(line)
    except Exception:  # noqa: BLE001
        return [{"type": "raw", "text": line}]
    kind = msg.get("type")
    out: list[dict] = []
    if kind == "assistant":
        for part in (msg.get("message") or {}).get("content") or []:
            if part.get("type") == "text" and part.get("text"):
                out.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "tool_use":
                out.append({"type": "tool", "name": part.get("name"),
                            "input": part.get("input")})
    elif kind == "result":
        out.append({"type": "done", "text": msg.get("result") or "",
                    "cost_usd": msg.get("total_cost_usd"),
                    "session_id": msg.get("session_id")})
    elif kind == "system" and msg.get("subtype") == "init":
        out.append({"type": "start", "session_id": msg.get("session_id"),
                    "model": msg.get("model"), "cwd": msg.get("cwd")})
    return out


def _parse_codex(line: str) -> list[dict]:
    """codex exec --json: JSONL events; shapes vary by version, so be lenient."""
    try:
        msg = json.loads(line)
    except Exception:  # noqa: BLE001
        return [{"type": "raw", "text": line}]
    body = msg.get("msg") if isinstance(msg.get("msg"), dict) else msg
    kind = body.get("type") or msg.get("type") or ""
    if "delta" in kind and body.get("delta"):
        return [{"type": "text", "text": body["delta"]}]
    if kind in ("agent_message", "assistant_message") and body.get("message"):
        return [{"type": "text", "text": body["message"]}]
    if "command" in kind and body.get("command"):
        return [{"type": "tool", "name": "shell", "input": body.get("command")}]
    if kind in ("task_complete", "turn_complete", "session_complete"):
        return [{"type": "done", "text": body.get("last_agent_message") or ""}]
    return []


def parse_line(fmt: str, line: str) -> list[dict]:
    line = line.rstrip("\n")
    if not line.strip():
        return []
    if fmt == "claude-stream-json":
        return _parse_claude(line)
    if fmt == "codex-jsonl":
        return _parse_codex(line)
    return [{"type": "text", "text": line + "\n"}]


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #
class Run:
    """One agent invocation, kept so a dropped panel can reattach to it."""

    def __init__(self, run_id: str, agent: str, prompt: str, cwd: str):
        self.id = run_id
        self.agent = agent
        self.prompt = prompt
        self.cwd = cwd
        self.started = time.time()
        self.events: list[dict] = []
        self.done = False
        self.error: Optional[str] = None
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.session_id: Optional[str] = None
        self._waiters: list[asyncio.Queue] = []

    def emit(self, ev: dict) -> None:
        ev["i"] = len(self.events)
        self.events.append(ev)
        if ev.get("session_id"):
            self.session_id = ev["session_id"]
        for q in self._waiters:
            q.put_nowait(ev)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._waiters.append(q)
        return q

    def summary(self) -> dict:
        return {"id": self.id, "agent": self.agent, "cwd": self.cwd,
                "started": self.started, "done": self.done, "error": self.error,
                "session_id": self.session_id, "events": len(self.events),
                "prompt": self.prompt[:200]}


RUNS: dict[str, Run] = {}
MAX_RUNS = 40


async def start(agent: str, prompt: str, cwd: str = "", session_id: str = "") -> Run:
    """Spawn an agent and stream its output into a Run."""
    conf = roster()
    runners = conf["runners"]
    name = agent or conf["default"]
    r = runners.get(name)
    if not r:
        raise ValueError(f"未配置 agent '{name}'（可用：{', '.join(runners) or '无'}）。"
                         f"跑一次 `wb agents --detect` 重新探测。")
    if not r.get("available"):
        raise ValueError(f"agent '{name}' 的可执行文件不在了（{r.get('path')}）。"
                         f"重新探测：`wb agents --detect`")

    workdir = os.path.expanduser(cwd or r.get("cwd") or DEFAULT_CWD)
    if not Path(workdir).is_dir():
        raise ValueError(f"工作目录不存在：{workdir}")

    argv = [r["path"], *list(r.get("args") or [])]
    spec = KNOWN.get(name) or {}
    if session_id and spec.get("resume_args"):
        argv += [*spec["resume_args"], session_id]
    argv.append(prompt)

    run = Run(uuid.uuid4().hex[:12], name, prompt, workdir)
    RUNS[run.id] = run
    for old in list(RUNS)[:-MAX_RUNS]:          # keep the table bounded
        if RUNS[old].done:
            RUNS.pop(old, None)

    run.emit({"type": "start", "agent": name, "cwd": workdir,
              "argv": argv[:-1] + ["<prompt>"]})
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=workdir, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
    run.proc = proc
    asyncio.create_task(_pump(run, proc, r.get("format") or "text"))
    return run


async def _pump(run: Run, proc, fmt: str) -> None:
    async def read_out():
        assert proc.stdout
        async for raw in proc.stdout:
            for ev in parse_line(fmt, raw.decode("utf-8", "replace")):
                run.emit(ev)

    async def read_err():
        assert proc.stderr
        async for raw in proc.stderr:
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                run.emit({"type": "stderr", "text": text})

    try:
        await asyncio.gather(read_out(), read_err())
        code = await proc.wait()
        if code != 0:
            run.error = f"agent 退出码 {code}"
    except Exception as e:  # noqa: BLE001
        run.error = str(e)
    finally:
        run.done = True
        run.emit({"type": "end", "error": run.error})


async def stream(run: Run, from_index: int = 0) -> AsyncIterator[str]:
    """NDJSON of a run's events: replay what happened, then follow live."""
    q = run.subscribe()
    for ev in run.events[from_index:]:
        yield json.dumps(ev, ensure_ascii=False) + "\n"
    if run.done:
        return
    while True:
        ev = await q.get()
        yield json.dumps(ev, ensure_ascii=False) + "\n"
        if ev.get("type") == "end":
            return


def stop(run_id: str) -> bool:
    run = RUNS.get(run_id)
    if not run or run.done or not run.proc:
        return False
    try:
        run.proc.terminate()
    except ProcessLookupError:
        return False
    return True

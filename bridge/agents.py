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
import subprocess
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
        "system_prompt_arg": "--append-system-prompt",
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

# One stream-json event is one line, and a line carrying a page's HTML or a big
# tool result is routinely megabytes.
STREAM_LIMIT = 16 * 1024 * 1024


# --------------------------------------------------------------------------- #
# the briefing every panel-launched agent gets
# --------------------------------------------------------------------------- #
# Without this, `claude -p "把当前页面存到 Evernote"` is a bare sentence: the agent
# has no idea it was launched from a browser side panel or which page the user is
# looking at. Observed in a real run — it opened with `osascript` to ask Chrome
# what was on screen, then fetched the page again through a REST API, and never
# touched web-bridge once. The whole point of this extension is that the page is
# already open and logged in; the agent has to be told that.
PANEL_BRIEF = """你正在 web-bridge 的浏览器侧栏里被调用，不是在终端里。

用户此刻正在看的页面：
  标题：{title}
  URL：{url}

硬性要求：
1. 需要读取或操作这个页面时，**必须**走 web-bridge 的 MCP 工具：
   `web_capabilities`（看这个页面有哪些现成能力）、`web_run_capability`（运行能力，
   例如 extract-article 抽正文、extract-tables 抽表格）、`web_exec`（在页面 MAIN world
   执行 JS）。页面是用户已登录的，JS 注入可以直接拿到内容和登录态接口。
2. **不要**用 osascript / AppleScript 去问浏览器开着什么，URL 就在上面。
   **不要**绕开这个页面另找 REST API、另开标签页抓“同样”的内容——那可能和用户眼前看到的
   不是一回事（登录态、权限、动态渲染都可能不同）。
3. 顺序是：先用 web-bridge 把数据取出来，再做后续处理（存档、总结、发送、写文件…）。
4. 动作要收敛。优先一两次工具调用拿到数据就往下走，不要长时间翻本地文件和 skill 源码——
   用户在侧栏里等着看结果。

**用户要你改这个页面时（美化、去广告、加按钮、抽数据…），做这两步：**
  a. 用 `web_exec` **把脚本真的跑上去**，让用户立刻在页面上看到效果。只探查不动手 =
     用户眼里什么都没发生。探查一两次就够，别反复看 DOM。
  b. 把这段 JS **贴在回答里**（```js 代码块），用户要能看见你写了什么。

**不要自己保存。** 面板会在你的代码块下面给两个按钮（「存为 Page Tool」/「存为 Agent Tool」），
存不存、存去哪、什么时候存由用户决定——他很可能还要接着让你改几轮。

用户**明确要求保存**时，先分清是给谁用的，两个库对应两个工具：

| 用户的话 | 存去哪 | 用什么工具 |
|---|---|---|
| 「存起来」「保存这个脚本」「以后我还要用」 | **Page Tools**（给人用：他自己点运行、开自动运行、导出成书签） | `web_save_page_script` |
| 「存到 Agent Tools」「存进你的能力库」「给你自己以后用」「做成一个能力」 | **Agent Tools**（给你用：以后 `web_capabilities` 能发现并调用） | `web_save_capability` |

分不清就问一句，别猜。**两个都不是默认的**——用户没说保存就别存。

（历史坑：用户说「把上面这个创建到脚本库」，当时只有 `web_save_page_script` 一个出口，
于是一个本该给 agent 复用的能力被存进了用户的页面脚本里。）

用户已有的页面脚本用 `web_page_scripts` 列出（**带完整代码**，别去磁盘上翻文件找），
要改就用 `web_save_page_script` 带上原 id，合并/替换掉的旧脚本用
`web_delete_page_script` 删掉——留着它会和新脚本一起自动运行。

写页面脚本的约定：函数体写法，可以 `await`，`return` 的值会显示给用户；
样式类改动要能重复执行不叠加（先判断元素在不在、加个标记位）。

**代码第一行写一句 `// 说明：这个脚本做什么`**（改进版就写这轮新增了什么能力）。
用户保存时面板会把它存成脚本说明，脚本列表里只显示名字和这句话——他不看代码，
靠这句话认出这是干嘛的。

如果用户的要求不够具体（例如只说「帮我写段 JS」），**先看页面再给一个具体方案并直接做**，
不要停下来只问一句"你想让它做什么"——用户已经把页面摆在你面前了。
"""


def available_tools_block(url: str) -> str:
    """List the tools that already exist for THIS page, inside the briefing.

    Discovery was a tool call the agent had to think of making, and a list of
    six generic capabilities is easy to skim past on the way to writing fresh
    JS. Putting the site-specific ones in front of it — by name, with what they
    do and what they take — removes the step entirely: the agent cannot fail to
    notice a tool it was handed.
    """
    try:
        import toolsearch
        site = [t for t in toolsearch.search("", url, limit=4, include_generic=False)
                if t["on_this_page"]]
    except Exception:  # noqa: BLE001
        return ""
    if not site:
        return ""
    # Ranked and capped: a briefing is context the user pays for on every turn,
    # so this lists the few best rather than everything that matches.
    lines = ["", "**这个页面已经有现成的 Agent Tools，优先用它们，别重写：**"]
    for t in site:
        params = ", ".join(t["params"])
        used = f"（用过 {t['runs']} 次）" if t["runs"] else ""
        lines.append(f"- `{t['id']}` — {t['title']}{used}"
                     f"{'  参数：' + params if params else ''}")
        if t["summary"]:
            lines.append(f"    {t['summary']}")
    lines += ["",
              "用 `web_run_capability` 调。**别的页面的工具也可能正合用**——"
              "按你要做的事去搜：`web_find_tool`（它按相关性和过往成功率排序，不受当前网址限制）。",
              "工具跑了但结果不对，用 `web_rate_tool` 标一下，以后它就会往后排。"]
    return "\n".join(lines)


def adhoc_hint(url: str) -> str:
    """If this site keeps getting hand-written JS and has no tool, say so."""
    try:
        import journal
        import capabilities
        host = journal.host_of(url)
        if not host:
            return ""
        if any(c.get("match") != ["*"] for c in capabilities.for_url(url)):
            return ""                       # it has tools; that is the other problem
        stats = journal.usage_stats(30, host)
        gap = next((g for g in stats.get("gaps", []) if g["host"] == host), None)
        # Writing a page script is dozens of distinct one-off probes. Telling the
        # agent to "save this as a capability" because of that is noise — a real
        # gap is the SAME work done repeatedly.
        if not gap or gap["looks_like_authoring"] or gap["repeats"] < 2:
            return ""
        return ("\n**注意**：这个站点最近 30 天被现写了 " + str(stats["adhoc_execs"]) +
                " 次 JS，却没有任何 Agent Tool。这活儿要是还会再做，"
                "做完主动问用户：要不要把它存成 Agent Tool（`web_save_capability`），"
                "下次就不用从头探页面了。\n")
    except Exception:  # noqa: BLE001
        return ""


def panel_brief(context: Optional[dict]) -> str:
    """The briefing text for a run started from the side panel."""
    c = context or {}
    if not c.get("url"):
        return ""
    return (PANEL_BRIEF.format(title=c.get("title") or "(无标题)", url=c["url"])
            + available_tools_block(c["url"])
            + adhoc_hint(c["url"]))


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
            data = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
    data["agents"] = block
    config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
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
RUN_DIR = Path(os.environ.get("WEB_BRIDGE_STATE", str(config.CONFIG_PATH.parent))) / "runs"
RUN_KEEP = 40                       # newest N kept on disk


class Run:
    """One agent invocation, kept so a dropped panel can reattach to it.

    Also written to disk. Runs used to live only in memory, so restarting the
    bridge — which happens on every code change — killed whatever was in flight
    AND erased the history: the panel reattached to an id that no longer existed
    and showed the user nothing at all for work they had waited minutes for.
    """

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

    def as_record(self) -> dict:
        return {"id": self.id, "agent": self.agent, "prompt": self.prompt,
                "cwd": self.cwd, "started": self.started, "done": self.done,
                "error": self.error, "session_id": self.session_id,
                "events": self.events}

    def persist(self) -> None:
        """Snapshot to disk. Cheap enough per event for a panel-driven agent."""
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            path = RUN_DIR / f"{self.id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.as_record(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def emit(self, ev: dict) -> None:
        ev["i"] = len(self.events)
        self.events.append(ev)
        # snapshot as it goes, not only at the end: a run killed mid-flight (a
        # service restart) still leaves the user everything it had produced
        if len(self.events) % 5 == 0:
            self.persist()
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


def restore_runs() -> int:
    """Reload runs from disk at startup.

    A run still marked running belongs to a process that is gone — its
    subprocess died with the old server — so it is closed out with an
    explanation. Silence is what the panel showed before, which reads as "the
    agent did nothing" rather than "the bridge restarted under you".
    """
    if not RUN_DIR.is_dir():
        return 0
    files = sorted(RUN_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for stale in files[RUN_KEEP:]:
        stale.unlink(missing_ok=True)
    loaded = 0
    for path in files[:RUN_KEEP]:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        run = Run(rec["id"], rec.get("agent", ""), rec.get("prompt", ""), rec.get("cwd", ""))
        run.started = rec.get("started", time.time())
        run.events = rec.get("events", [])
        run.session_id = rec.get("session_id")
        run.error = rec.get("error")
        run.done = True
        if not rec.get("done"):
            run.error = run.error or "这次运行被 bridge 重启中断了（服务重启会杀掉正在跑的 agent）"
            run.events.append({"type": "end", "error": run.error, "i": len(run.events)})
            run.persist()
        RUNS[run.id] = run
        loaded += 1
    return loaded


def live_runs() -> list[str]:
    """Ids of runs still going — a restart would kill these."""
    return [r.id for r in RUNS.values() if not r.done]
MAX_RUNS = 40


async def start(agent: str, prompt: str, cwd: str = "", session_id: str = "",
                context: Optional[dict] = None) -> Run:
    """Spawn an agent and stream its output into a Run.

    `context` carries what the side panel knows and the agent cannot guess —
    which page the user is looking at, and that web-bridge is how to reach it.
    """
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

    brief = panel_brief(context)
    if brief:
        if spec.get("system_prompt_arg"):
            # claude keeps this out of the transcript, so a resumed conversation
            # is not re-briefed on every turn
            argv += [spec["system_prompt_arg"], brief]
        else:
            prompt = brief + "\n---\n\n" + prompt
    argv.append(prompt)

    run = Run(uuid.uuid4().hex[:12], name, prompt, workdir)
    RUNS[run.id] = run
    for old in list(RUNS)[:-MAX_RUNS]:          # keep the table bounded
        if RUNS[old].done:
            RUNS.pop(old, None)

    run.emit({"type": "start", "agent": name, "cwd": workdir,
              "argv": argv[:-1] + ["<prompt>"]})
    # asyncio's StreamReader defaults to a 64KB line limit, and claude's
    # stream-json puts one whole event on one line — a large tool result blew
    # past it and killed the run with "Separator is not found, and chunk exceed
    # the limit", losing everything the agent had already done.
    # CREATE_NO_WINDOW: the agent CLIs are console programs, and the bridge
    # normally runs under pythonw.exe, which has NO console of its own — so
    # Windows allocates a fresh console for each child and a terminal window
    # pops up in the user's face on every sidebar message. The flag is
    # Windows-only and does not exist on macOS/Linux.
    # Imported here, not at module scope: service.py imports THIS module, so a
    # top-level `import service` would be a cycle. By call time it is loaded.
    import service
    extra = {}
    if service.IS_WINDOWS:
        extra["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=workdir, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
        limit=STREAM_LIMIT, **extra)
    run.proc = proc
    asyncio.create_task(_pump(run, proc, r.get("format") or "text"))
    return run


async def _pump(run: Run, proc, fmt: str) -> None:
    async def read_out():
        assert proc.stdout
        while True:
            try:
                raw = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as e:
                # even past the raised limit, drop the offending line rather than
                # abandoning a run the user is watching
                run.emit({"type": "stderr", "text": f"(跳过一行超长输出: {e})"})
                continue
            if not raw:
                break
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
        run.persist()


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

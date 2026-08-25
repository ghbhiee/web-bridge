#!/usr/bin/env python3
"""Register (or remove) the web-bridge MCP server across the local agents.

Each agent stores MCP servers in its own format, so this writes each one
natively and idempotently:

  claude   ~/.claude.json                 mcpServers.<name> = {command, args}
  codex    ~/.codex/config.toml           [mcp_servers.<name>] command/args
  hermes   ~/.hermes/config.yaml          mcp_servers.<name> = {command, args, enabled}
  openclaw / dsh                          discovered at runtime (see --list)

usage:
  register_mcp.py --list        # show what each agent has now
  register_mcp.py               # register into every supported agent
  register_mcp.py --remove
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "web-bridge"
COMMAND = sys.executable or "python3"
ARGS = [str(HERE / "mcp_server.py")]

CLAUDE = Path.home() / ".claude.json"
CODEX = Path.home() / ".codex/config.toml"
HERMES = Path.home() / ".hermes/config.yaml"


def backup(p: Path):
    if p.is_file():
        shutil.copy2(p, p.with_suffix(p.suffix + ".wb-bak"))


# --------------------------------------------------------------------------- #
def claude(remove=False, show=False):
    if not CLAUDE.is_file():
        return "claude: ~/.claude.json 不存在"
    data = json.loads(CLAUDE.read_text())
    servers = data.setdefault("mcpServers", {})
    if show:
        return f"claude: {list(servers.keys())}"
    backup(CLAUDE)
    if remove:
        servers.pop(NAME, None)
    else:
        servers[NAME] = {"command": COMMAND, "args": ARGS}
    CLAUDE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return f"claude: {'removed' if remove else 'registered'}"


def codex(remove=False, show=False):
    if not CODEX.is_file():
        return "codex: config.toml 不存在"
    text = CODEX.read_text()
    if show:
        return "codex: " + str(re.findall(r"\[mcp_servers\.\"?([\w-]+)\"?\]", text))
    backup(CODEX)
    # the table key is quoted (the name contains a dash), so match both forms
    block_re = re.compile(r'\n?\[mcp_servers\."?%s"?\][^\[]*' % re.escape(NAME), re.S)
    text = block_re.sub("\n", text)
    if not remove:
        args_toml = ", ".join(json.dumps(a) for a in ARGS)
        text = text.rstrip() + (
            f'\n\n[mcp_servers."{NAME}"]\ncommand = {json.dumps(COMMAND)}\nargs = [{args_toml}]\n'
        )
    CODEX.write_text(text)
    return f"codex: {'removed' if remove else 'registered'}"


def hermes(remove=False, show=False):
    if not HERMES.is_file():
        return "hermes: config.yaml 不存在"
    lines = HERMES.read_text().split("\n")
    # locate the `mcp_servers:` top-level block
    start = next((i for i, l in enumerate(lines) if l.startswith("mcp_servers:")), None)
    if start is None:
        return "hermes: 未找到 mcp_servers 段"
    end = start + 1
    while end < len(lines) and (lines[end].startswith(" ") or not lines[end].strip()):
        end += 1
    block = lines[start + 1:end]
    if show:
        names = [l.strip().rstrip(":") for l in block if re.match(r"^  \S+:", l)]
        return f"hermes: {names}"
    backup(HERMES)
    # drop an existing entry for NAME
    out, skipping = [], False
    for l in block:
        if re.match(r"^  %s:" % re.escape(NAME), l):
            skipping = True
            continue
        if skipping and re.match(r"^  \S", l):
            skipping = False
        if not skipping:
            out.append(l)
    if not remove:
        entry = [f"  {NAME}:", f"    command: {COMMAND}", "    args:"]
        entry += [f"      - {a}" for a in ARGS]
        entry += ["    enabled: true"]
        # insert right after the header, keeping trailing blank lines at the end
        while out and not out[0].strip():
            out.pop(0)
        out = entry + out
    HERMES.write_text("\n".join(lines[:start + 1] + out + lines[end:]))
    return f"hermes: {'removed' if remove else 'registered'}"


OPENCLAW = Path.home() / ".openclaw/openclaw.json"


def openclaw(remove=False, show=False):
    if not OPENCLAW.is_file():
        return "openclaw: openclaw.json 不存在"
    data = json.loads(OPENCLAW.read_text())
    servers = data.setdefault("mcp", {}).setdefault("servers", {})
    if show:
        return f"openclaw: {list(servers.keys())}"
    backup(OPENCLAW)
    if remove:
        servers.pop(NAME, None)
    else:
        servers[NAME] = {"command": COMMAND, "args": ARGS}
    OPENCLAW.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return f"openclaw: {'removed' if remove else 'registered'}"


AGENTS = [claude, codex, hermes, openclaw]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    for fn in AGENTS:
        try:
            print(fn(remove=a.remove, show=a.list))
        except Exception as e:  # noqa: BLE001
            print(f"{fn.__name__}: ERROR {e}")
    if not a.list:
        print(f"\ncommand: {COMMAND} {' '.join(ARGS)}")
        print("重启对应 agent 后生效。")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""A standalone mock extension SW: connects to the bridge WS and answers
commands with canned data. For driving the CLI/MCP without a real browser."""
import asyncio
import json
import os
import sys

import websockets

import config

# `client=mock` is not cosmetic: the bridge refuses a mock on the live port,
# because the hub has one extension slot and a mock takes it away from the real
# extension (which reconnects, evicting the mock, forever).
WS = f"ws://{config.HOST}:{config.PORT}/ws/ext?token={config.TOKEN}&client=mock"


def _refuse_live_port() -> None:
    if config.PORT == 8790 and os.environ.get("WEB_BRIDGE_ALLOW_MOCK") != "1":
        sys.exit("拒绝连接生产 bridge（端口 8790）——mock 会顶掉真扩展。\n"
                 "用 bridge/run_tests.sh，或自己设 WEB_BRIDGE_PORT/WEB_BRIDGE_STATE 起一次性实例。")


async def serve():
    async for ws in websockets.connect(WS):  # auto-reconnect
        try:
            await ws.send(json.dumps({"type": "hello", "info": {"mock": True}}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "command":
                    continue
                a, p, rid = msg["action"], msg.get("payload") or {}, msg["id"]
                if a == "exec":
                    data = {"result": {"echo": p.get("args"), "code": p.get("code"), "site": p.get("site")}}
                elif a == "tabs":
                    data = {"tabs": [{"id": 1, "url": "https://example.com/", "title": "Example", "active": True}]}
                elif a == "open":
                    data = {"tabId": 2, "url": p.get("url")}
                elif a == "adapter":
                    data = {"result": {"adapter": p.get("site"), "method": p.get("method"),
                                       "text": "mock answer", "images": []}}
                elif a == "reload":
                    data = {"reloading": True}
                else:
                    await ws.send(json.dumps({"type": "result", "id": rid, "ok": False, "error": f"mock: {a}?"}))
                    continue
                await ws.send(json.dumps({"type": "result", "id": rid, "ok": True, "data": data}))
        except websockets.ConnectionClosed:
            continue


if __name__ == "__main__":
    _refuse_live_port()
    asyncio.run(serve())

"""Shared config for the web-bridge server / CLI / MCP.

Reads ~/.config/web-bridge/config.json (chmod 600). The token gates every HTTP
and WebSocket call. Environment variables override the file for one-off use.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get("WEB_BRIDGE_CONFIG", str(Path.home() / ".config/web-bridge/config.json"))
)


def load() -> dict:
    data: dict = {}
    if CONFIG_PATH.is_file():
        try:
            # encoding is explicit because gen_ext_config.py writes this file
            # as utf-8: without it Windows reads it back in the locale
            # codepage and any non-ascii value (a site home, an agent cwd
            # under a Chinese user folder) comes back as mojibake --
            # silently, with no exception to notice.
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault("host", os.environ.get("WEB_BRIDGE_HOST", "127.0.0.1"))
    env_port = os.environ.get("WEB_BRIDGE_PORT")
    if env_port:
        data["port"] = int(env_port)
    data.setdefault("port", 8790)
    data.setdefault("sites", {})
    env_token = os.environ.get("WEB_BRIDGE_TOKEN")
    if env_token:
        data["token"] = env_token
    return data


CFG = load()
HOST: str = CFG["host"]
PORT: int = int(CFG["port"])
TOKEN: str = CFG.get("token", "")
SITES: dict = CFG.get("sites", {})


# Pages we refuse to touch. Injecting into a bank / password manager / health
# portal is never worth the convenience, and an agent shouldn't be able to do it
# by accident (or by following instructions it read on some page). Patterns are
# substring-matched against the URL, case-insensitively.
DEFAULT_BLOCKLIST = [
    "accounts.google.com", "myaccount.google.com",
    "password", "passkey", "1password.com", "bitwarden.com", "lastpass.com",
    "keychain", "chrome://", "chrome-extension://", "about:",
    "bank", "banking", "paypal.com", "stripe.com/dashboard",
    "icbc.com.cn", "ccb.com", "abchina.com", "boc.cn", "cmbchina.com",
    "alipay.com", "pay.weixin", "wallet",
    "health", "patient", "medical",
]
BLOCKLIST: list = CFG.get("blocklist", DEFAULT_BLOCKLIST)


def is_blocked(url: str) -> str:
    """Return the matching pattern if this URL is off-limits, else ''."""
    if not url:
        return ""
    u = url.lower()
    for pat in BLOCKLIST:
        if pat.lower() in u:
            return pat
    return ""


def save_sites() -> None:
    """Persist the (possibly runtime-modified) sites map back to config.json."""
    data = {}
    if CONFIG_PATH.is_file():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["sites"] = SITES
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)


def base_url() -> str:
    return os.environ.get("WEB_BRIDGE_URL", f"http://{HOST}:{PORT}")

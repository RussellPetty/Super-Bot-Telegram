#!/usr/bin/env python3
"""Helper for install.sh: configure a Telegram bot via the Bot API.

Sub-commands:
    configure <token>
        Validate token, clear any stale webhook, set the bot's command menu,
        name, description, and short description. Prints key=value lines
        (username, first_name) on stdout.

    detect-user <token> [timeout_seconds]
        Drain any pending updates, then long-poll getUpdates until the bot
        receives a message. Prints user_id, user_first_name, user_username
        on stdout when found.

Uses stdlib only — no httpx dependency required (install.sh runs this before
the venv exists).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"

COMMANDS = [
    {"command": "new",     "description": "Reset session and clear history"},
    {"command": "stop",    "description": "Stop the running AI process"},
    {"command": "model",   "description": "Pick a model for the current backend"},
    {"command": "codex",   "description": "Switch to Codex backend"},
    {"command": "ollama",  "description": "Switch to Ollama backend"},
    {"command": "term",    "description": "Run next message as shell command"},
    {"command": "status",  "description": "Show current mode and session"},
    {"command": "attach",  "description": "Resume a Claude session by id"},
    {"command": "restart", "description": "Restart the bot process"},
]

DESCRIPTION = (
    "Personal AI bot. Bridges your chats to Claude Code, Codex, or a local "
    "Ollama model running on your own machine. "
    "Source: https://github.com/RussellPetty/Super-Bot-Telegram"
)

SHORT_DESCRIPTION = "Your personal Claude / Codex / Ollama bridge."


def call(token: str, method: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    url = API.format(token=token, method=method)
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def configure(token: str) -> int:
    me = call(token, "getMe")
    if not me.get("ok"):
        print(f"error: {me.get('description', 'invalid token')}", file=sys.stderr)
        return 1
    res = me["result"]
    username = res.get("username", "")
    first_name = res.get("first_name", "")
    print(f"username={username}", flush=True)
    print(f"first_name={first_name}", flush=True)

    steps = (
        ("deleteWebhook",         {"drop_pending_updates": False}),
        ("setMyCommands",         {"commands": COMMANDS}),
        ("setMyDescription",      {"description": DESCRIPTION}),
        ("setMyShortDescription", {"short_description": SHORT_DESCRIPTION}),
    )
    for method, payload in steps:
        r = call(token, method, payload)
        if not r.get("ok"):
            print(f"warn: {method} failed: {r.get('description')}", file=sys.stderr)

    return 0


def detect_user(token: str, timeout: int = 180) -> int:
    # Drain any pending updates so we only react to fresh messages.
    pending = call(token, "getUpdates", {"timeout": 0, "limit": 100, "offset": -1})
    if pending.get("ok"):
        results = pending.get("result") or []
        if results:
            last_id = max(u["update_id"] for u in results)
            call(token, "getUpdates", {"timeout": 0, "offset": last_id + 1})

    deadline = time.time() + timeout
    while time.time() < deadline:
        wait = min(25, max(1, int(deadline - time.time())))
        r = call(token, "getUpdates", {"timeout": wait, "limit": 1}, timeout=wait + 10)
        if not r.get("ok"):
            time.sleep(2)
            continue
        for upd in r.get("result", []):
            msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post")
            sender = (msg or {}).get("from") or {}
            if sender.get("id"):
                print(f"user_id={sender['id']}", flush=True)
                print(f"user_first_name={sender.get('first_name', '')}", flush=True)
                print(f"user_username={sender.get('username', '')}", flush=True)
                return 0

    print("error: timed out waiting for first message", file=sys.stderr)
    return 2


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: telegram-setup.py {configure|detect-user} <token> [timeout]", file=sys.stderr)
        return 1
    cmd, token = sys.argv[1], sys.argv[2]
    if cmd == "configure":
        return configure(token)
    if cmd == "detect-user":
        timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 180
        return detect_user(token, timeout)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

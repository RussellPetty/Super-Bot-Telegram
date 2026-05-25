#!/usr/bin/env python3
"""Post a Homi reply on a support ticket.

Resolution order for where the reply gets POSTed:
  1. The ticket's `reply_url` recorded in ticket_threads.json (multi-codebase
     webhook flow) — Authorization is `Bearer <reply_token>` from the same
     record (or none if the token wasn't supplied).
  2. Legacy broker-marketplace flow: `{base_url}/api/support-tickets/{id}/homi-reply`
     authorized by SUPABASE_SERVICE_ROLE_KEY from the bot's environment.

Usage:
    echo "body text" | python3 /path/to/homi_reply.py --ticket-id <uuid>
    python3 /path/to/homi_reply.py --ticket-id <uuid> --text "body"

Reads body from --text or stdin (stdin preferred for multi-line / markdown).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

DEFAULT_BASE_URL = "https://broker-marketplace.com"
STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticket_threads.json")


def _lookup_ticket(ticket_id: str) -> dict:
    try:
        with open(STORE_PATH) as f:
            store = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"warn: couldn't read ticket store at {STORE_PATH}: {e}", file=sys.stderr)
        return {}
    return store.get(ticket_id) or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Post a Homi reply on a support ticket.")
    parser.add_argument("--ticket-id", required=True, help="Support ticket UUID")
    parser.add_argument("--text", default=None, help="Reply body (omit to read from stdin)")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SUPPORT_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        help="Legacy fallback base URL (broker-marketplace flow)",
    )
    args = parser.parse_args()

    body = args.text if args.text is not None else sys.stdin.read()
    body = body.strip()
    if not body:
        print("error: empty reply body", file=sys.stderr)
        return 2

    info = _lookup_ticket(args.ticket_id)
    reply_url = info.get("reply_url")

    headers = {"Content-Type": "application/json"}
    payload = {"content": body, "ticket_id": args.ticket_id}

    if reply_url:
        # New multi-codebase flow: POST to the ticket's own reply_url.
        token = info.get("reply_token") or ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = reply_url
    else:
        # Legacy broker-marketplace flow.
        token = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not token:
            print(
                "error: no reply_url recorded for this ticket and "
                "SUPABASE_SERVICE_ROLE_KEY isn't set — can't determine where to POST.",
                file=sys.stderr,
            )
            return 2
        headers["Authorization"] = f"Bearer {token}"
        url = f"{args.base_url}/api/support-tickets/{args.ticket_id}/homi-reply"

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=30.0)
    except Exception as e:
        print(f"error: request to {url} failed: {e}", file=sys.stderr)
        return 1

    if resp.status_code >= 400:
        print(f"error: {resp.status_code} {resp.text[:500]}", file=sys.stderr)
        return 1

    print(resp.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

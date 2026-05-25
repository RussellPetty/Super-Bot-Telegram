#!/usr/bin/env python3
"""Post a Homi reply on a support ticket.

Usage:
    echo "body text" | python3 ~/claude-telegram/homi_reply.py --ticket-id <uuid>
    python3 ~/claude-telegram/homi_reply.py --ticket-id <uuid> --text "body"

Reads body from --text or stdin (stdin preferred for multi-line / markdown).
Reuses the bot's env: SUPABASE_SERVICE_ROLE_KEY, SUPPORT_API_BASE_URL.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

DEFAULT_BASE_URL = "https://mortgagemarketplace.ai"


def main() -> int:
    parser = argparse.ArgumentParser(description="Post a Homi reply on a support ticket.")
    parser.add_argument("--ticket-id", required=True, help="Support ticket UUID")
    parser.add_argument("--text", default=None, help="Reply body (omit to read from stdin)")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SUPPORT_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        help="Web app base URL (defaults to $SUPPORT_API_BASE_URL or production)",
    )
    args = parser.parse_args()

    body = args.text if args.text is not None else sys.stdin.read()
    body = body.strip()
    if not body:
        print("error: empty reply body", file=sys.stderr)
        return 2

    token = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not token:
        print("error: SUPABASE_SERVICE_ROLE_KEY not set in env", file=sys.stderr)
        return 2

    url = f"{args.base_url}/api/support-tickets/{args.ticket_id}/homi-reply"
    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"content": body},
            timeout=30.0,
        )
    except Exception as e:
        print(f"error: request failed: {e}", file=sys.stderr)
        return 1

    if resp.status_code >= 400:
        print(f"error: {resp.status_code} {resp.text[:500]}", file=sys.stderr)
        return 1

    print(resp.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

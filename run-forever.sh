#!/bin/bash
# Keeps the bot running forever, restarting on crash after 5 seconds

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

while true; do
    # Re-source .env each iteration so edits are picked up without restarting the supervisor.
    if [ -f "$SCRIPT_DIR/.env" ]; then
        set -a
        source "$SCRIPT_DIR/.env"
        set +a
    fi
    echo "[$(date)] Starting bot..."
    # Prefer the venv installed by install.sh; fall back to system python3.
    if [ -x "$SCRIPT_DIR/venv/bin/python3" ]; then
        "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/bot.py"
    else
        python3 "$SCRIPT_DIR/bot.py"
    fi
    echo "[$(date)] Bot exited. Restarting in 5 seconds..."
    sleep 5
done

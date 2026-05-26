# CLAUDE.md — Setup runbook for AI agents

You've cloned **Super-Bot-Telegram**. This file tells you (Claude Code) how to
set it up. Read this before doing anything; it lists the steps that require
the human in the loop.

---

## What this is

A Telegram bot that bridges messages to Claude Code or Codex, plus an optional
HTTP webhook for support-ticket investigation that can serve any number of
codebases.

The "Ollama" backend choice is **not a separate runner** — it still drives the
Claude Code CLI, but with `ANTHROPIC_BASE_URL` pointed at the local Ollama
server (which exposes the Anthropic API) and `--model` set to an installed
Ollama model. You get the full agentic loop (tools, file edits, session resume);
the only thing that changes is which model produces the tokens. Requires
`ollama` ≥ 0.15 and a tool-capable local model with a ≥64k context window.

Key files:
- `bot.py` — the bot itself (PTB 21, asyncio, aiohttp for the webhook)
- `install.sh` — interactive installer
- `telegram-setup.py` — stdlib-only helper that drives the Telegram Bot API
- `homi_reply.py` — CLI Claude invokes from inside a support topic to POST a reply
- `run-forever.sh` — supervisor loop (re-sources `.env` each iteration)
- `ticket_threads.json` — runtime state for the webhook (gitignored)

---

## Setup (first-time install)

### Step 1 — Run the installer

```bash
./install.sh
```

It's interactive. You **cannot** finish it without the human:

| Prompt | What the human supplies |
|---|---|
| Backend (Claude / Codex / Ollama) | Their choice |
| Backend login | They must run `claude` or `codex login` in a separate terminal (browser OAuth) |
| Model | Their pick from the offered list |
| Telegram bot token | From @BotFather → `/newbot` (no programmatic way to issue tokens) |
| User id | **Auto-detected** — after token validation, the installer prints a `t.me/<botname>` link and waits up to 3 min for the human to tap Start |
| Default codebase dir (optional) | Path on their machine if they want support-ticket investigation |
| Telegram support group id (optional) | Negative number; only if they want support tickets |

If a prompt blocks (e.g. waiting for Start tap), don't time out — the user
might still be clicking. The Telegram auto-detect falls back to manual `@userinfobot`
entry if it times out.

### Step 2 — Smoke-check

After the installer finishes:

```bash
# Did .env actually get written?
test -s .env && echo "env OK"

# Does bot.py at least import cleanly?
./venv/bin/python3 -c "import bot"
```

Then ask the human to message the bot in Telegram — it should reply.

If support tickets were enabled:

```bash
curl -fsS http://127.0.0.1:9091/health
# → {"ok": true}
```

### Step 3 — Run persistently

The installer offers to start it foreground. For background:

```bash
nohup ./run-forever.sh > bot.stdout.log 2> bot.stderr.log &
```

`run-forever.sh` restarts the bot on crash and re-sources `.env` each loop,
so to apply env changes the human just edits `.env` and `kill`s the python
process (NOT the supervisor).

---

## Reconfiguring an existing install

`./install.sh` is idempotent. Re-running it:
- Reuses the existing bot token (or accepts a new one)
- Reuses the existing user id unless the human picks "Type manually" or "Auto-detect"
- Reuses the existing webhook token unless the human asks to regenerate
- Preserves any extra `.env` keys it doesn't recognise

So you can safely re-run it to change a single thing (e.g. the default model).

---

## Wiring a calling codebase to the support webhook

This is the **multi-codebase use case**: the bot is already running, the
human wants their web app (in a different repo) to POST support tickets to
it. Tell the human: "I need three things from you to wire this up — bot
host/port, webhook bearer token, and a shared secret your app will use to
verify Homi's replies coming back."

Then in the calling codebase, add three POSTs:

```ts
// 1. New ticket → bot opens a forum topic + runs Claude on the codebase
await fetch(`${BOT_BASE}/support-ticket`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${WEBHOOK_TOKEN}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({
    id: ticket.id,
    user_name: ticket.userName,
    user_email: ticket.userEmail,
    message: ticket.message,
    current_page: ticket.page,
    attachments: ticket.files.map(f => ({ url: f.publicUrl, filename: f.name })),
    project_dir: '/absolute/path/on/the/bot-host/to/this/codebase',
    reply_url: `${YOUR_APP_BASE}/api/webhooks/homi-reply`,
    reply_token: HOMI_REPLY_SHARED_SECRET,   // your app verifies this on inbound
    metadata: { plan: user.plan, build: process.env.BUILD_SHA },
  }),
});

// 2. End user replied on your side → push it into the topic
await fetch(`${BOT_BASE}/support-ticket/reply`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${WEBHOOK_TOKEN}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ ticket_id: ticket.id, content: reply.text, user_name: reply.userName }),
});

// 3. Ticket closed → bot deletes the topic
await fetch(`${BOT_BASE}/support-ticket/resolved`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${WEBHOOK_TOKEN}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ ticket_id: ticket.id }),
});
```

And **one inbound endpoint** in the calling codebase, for Homi's replies:

```ts
// POST /api/webhooks/homi-reply
// body: { ticket_id, content }
// headers: Authorization: Bearer <reply_token-you-sent-in-the-original-payload>
//
// Verify the bearer matches HOMI_REPLY_SHARED_SECRET, then deliver `content`
// to the end user however your app normally does (email, in-app note, etc).
```

State to expose:
- `BOT_BASE` (e.g. `https://bot.you.dev` or `http://127.0.0.1:9091`)
- `WEBHOOK_TOKEN` (the bot generated this during install; only the human knows it)
- `HOMI_REPLY_SHARED_SECRET` (you generate this for the calling codebase)

Keep all three in the calling codebase's env, never in source.

---

## When you need to ask the human

| Situation | Why |
|---|---|
| Bot token | Only @BotFather issues them; no API |
| Backend login | Browser OAuth; can't be automated |
| Telegram group id | They must add the bot to a forum group first |
| Webhook public URL | They decide between bind=0.0.0.0, cloudflared, ngrok, tailscale |
| Whether to regenerate tokens | Destructive; old web-app integrations break |

---

## Common gotchas

- **macOS bot + remote web app:** `bind=127.0.0.1` is unreachable. Either re-run
  install.sh and set `bind=0.0.0.0` (open the port) or front the port with
  `cloudflared tunnel --url http://localhost:9091`, ngrok, or tailscale-funnel.

- **Supergroup migration:** When Telegram promotes a basic group to a
  supergroup, the chat_id changes. Logs will contain
  `"Group migrated to supergroup. New chat id: X"` — update
  `SUPPORT_GROUP_ID` in `.env` to the new id.

- **`bot.stderr.log` growth:** Verbose tracebacks can push this into the
  hundreds of MB. Truncate periodically: `: > bot.stderr.log` (while the bot
  is running is fine; it's append-mode).

- **Two bots polling the same token:** Telegram only delivers `getUpdates` to
  one consumer at a time. If you see "terminated by other getUpdates request"
  errors, another instance is running (`pkill -f bot.py` and restart).

- **`telegramify_markdown` import errors:** Means the venv wasn't created or
  `requirements.txt` wasn't installed. Run `./venv/bin/pip install -r requirements.txt`.

---

## Files NOT to commit

`.gitignore` covers these but they're worth knowing about:
- `.env` — bot/user tokens
- `ticket_threads.json` — live ticket↔thread mapping, includes `reply_token`s
- `bot.*.log` — can contain user message text
- `venv/` — local Python deps

# Super-Bot-Telegram

A Telegram bot that bridges chat messages to your choice of AI backend:

- **Claude Code** — Anthropic's agentic `claude` CLI (best for code/tools)
- **Codex** — OpenAI's `codex` CLI
- **Ollama** — local models running on your own machine (private, free)

Streams tool calls and final responses back to Telegram with Markdown formatting,
voice transcription (Whisper), and optional TTS voice replies (Grok Ara).

## One-command install

```bash
curl -fsSL https://raw.githubusercontent.com/RussellPetty/Super-Bot-Telegram/master/install.sh | bash
```

The installer is interactive: it picks a backend, installs the CLI for it, walks
you through login, lets you choose a model, then handles the Telegram side via
the Bot API — validates your token, sets the bot's command menu and description,
and **auto-detects your user id** by waiting for you to tap Start in the bot's
chat. The only manual step is pasting the token from @BotFather.

To install into a custom directory:

```bash
CT_INSTALL_DIR=~/my-bot curl -fsSL https://raw.githubusercontent.com/RussellPetty/Super-Bot-Telegram/master/install.sh | bash
```

Re-run anytime to reconfigure (the installer is idempotent and preserves any
extra env vars you've added):

```bash
cd ~/super-bot-telegram && ./install.sh
```

## Manual install

If you'd rather skip the installer:

1. Clone & install Python deps in a venv:
   ```bash
   git clone https://github.com/RussellPetty/Super-Bot-Telegram.git
   cd Super-Bot-Telegram
   python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
   ```
2. Install your chosen backend (`npm i -g @anthropic-ai/claude-code`, `npm i -g @openai/codex`, or `brew install ollama`) and log in.
3. Copy `.env.example` → `.env` and fill in your Telegram bot token + user id.
4. Run `./run-forever.sh`.

## Running

```bash
./run-forever.sh                  # foreground, restarts on crash
nohup ./run-forever.sh > bot.stdout.log 2> bot.stderr.log &   # background
```

`run-forever.sh` re-sources `.env` on every restart, so editing `.env` and
killing the python process is enough to pick up changes.

## Commands inside Telegram

| Command | What it does |
|---|---|
| `/start` | Welcome message |
| `/new` | Reset session and clear chat history |
| `/stop` | Kill the running AI process |
| `/model` | List models for the current backend; reply with a number to pick |
| `/codex` | Switch this chat to Codex |
| `/ollama` | Switch this chat to Ollama |
| `/term` | Next message runs as a shell command |
| `/status` | Show current chat / session / mode |
| `/attach <id>` | Resume an existing Claude session |
| `/restart` | Restart the bot process |

To switch *back* to Claude from Codex/Ollama, run `/new` (also clears history)
or `/model` (preserves Codex history; only works from Codex).

## Backend cheat-sheet

| | Claude Code | Codex | Ollama |
|---|---|---|---|
| Authoring tools (Read/Edit/Bash) | yes | yes | no (chat only) |
| Cost | usage-billed | usage-billed | free (local) |
| Network required | yes | yes | no |
| Image input | yes | yes | no |
| File input | yes | yes | inlined as text |

The bot defaults each new chat to whatever `BOT_BACKEND=` is in `.env`. You can
still flip backends at runtime per-chat with `/codex` and `/ollama`.

## Support-ticket investigation (optional)

The bot can open a Telegram forum topic per support ticket and run Claude on
your codebase to diagnose it — driven entirely by webhooks from your web app.
No shared database required, and one bot instance can serve multiple
codebases (each ticket payload can override the project directory).

### Endpoints

All three require `Authorization: Bearer <SUPPORT_WEBHOOK_TOKEN>`. Bot returns
JSON `{ok, ticket_id, thread_id}`.

| Endpoint | When your app calls it | Body |
|---|---|---|
| `POST /support-ticket` | New ticket created | full ticket payload (see below) |
| `POST /support-ticket/reply` | End user replied | `{ticket_id\|thread_id, content, attachments, user_name}` |
| `POST /support-ticket/resolved` | Ticket closed | `{ticket_id\|thread_id}` |

### New-ticket payload

```jsonc
{
  "id":           "<uuid>",          // required, unique
  "user_name":    "Alice",
  "user_email":   "alice@example.com",
  "user_id":      "<your-id>",
  "ticket_type":  "bug",
  "current_page": "/upload",
  "device_type":  "iOS 17 / Safari",
  "message":      "Cannot upload PDFs over 10 MB",
  "metadata":     { "plan": "pro", "build": "abc123" }, // shown in intro
  "attachments":  [ {"url": "https://...", "filename": "screenshot.png"} ],
  "project_dir":  "/path/to/this-codebase",  // overrides SUPPORT_PROJECT_DIR
  "reply_url":    "https://your-app.com/webhooks/homi-reply",
  "reply_token":  "<shared-secret-for-the-reply-POST>"
}
```

### How replies from Homi flow back to your app

When the human in the support topic approves a reply, the bot POSTs to the
ticket's `reply_url` with `Authorization: Bearer <reply_token>` and body
`{ticket_id, content}`. Your app is responsible for emailing / notifying the
end user from there.

### State

Bot stores a `ticket_id → {thread_id, reply_url, reply_token, project_dir}`
map in `./ticket_threads.json` (gitignored). The new-ticket endpoint is
idempotent on `id` — duplicate POSTs return the existing thread instead of
opening a second one.

### Optional Supabase polling

If you're running the broker-marketplace setup, the installer can also wire
the Supabase poller as a fallback (the bot will read tickets from the
`support_tickets` table and dispatch them the same way). This is purely
optional and disabled by default.

## Env vars

See `.env.example` for the full list. Required:

- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `ALLOWED_USER_ID` — your numeric Telegram id (from @userinfobot)

Useful optional:

- `BOT_BACKEND` — `claude` (default) | `codex` | `ollama`
- `CLAUDE_MODEL`, `OLLAMA_MODEL`, `OLLAMA_HOST`
- `CLAUDE_WORKING_DIR` — default cwd for AI commands (defaults to `~`)
- `OPENAI_API_KEY` — enables voice-message transcription via Whisper
- `XAI_API_KEY` — enables TTS voice replies via Grok Ara
- `SUPPORT_PROJECT_DIR`, `SUPPORT_GROUP_ID`, `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE_KEY` — enable the support-ticket poller
- `SUPPORT_WEBHOOK_PORT`, `SUPPORT_WEBHOOK_TOKEN`, `SUPPORT_WEBHOOK_BIND`
  — enable the webhook listener on top of the poller

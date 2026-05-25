#!/usr/bin/env bash
# install.sh — interactive installer for Super-Bot-Telegram.
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/RussellPetty/Super-Bot-Telegram/master/install.sh | bash
#
# Re-run anytime to reconfigure.

set -euo pipefail

REPO_URL="https://github.com/RussellPetty/Super-Bot-Telegram.git"
DEFAULT_INSTALL_DIR="$HOME/super-bot-telegram"

# ── Bootstrap: if piped from curl, clone repo then re-exec from it ────────────
if [ ! -f "./bot.py" ] || [ ! -f "./install.sh" ]; then
    TARGET="${CT_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
    if [ -d "$TARGET/.git" ]; then
        echo "→ Existing repo at $TARGET, updating..."
        git -C "$TARGET" pull --ff-only
    else
        echo "→ Cloning $REPO_URL → $TARGET"
        git clone "$REPO_URL" "$TARGET"
    fi
    cd "$TARGET"
    chmod +x ./install.sh
    exec bash ./install.sh < /dev/tty "$@"
fi

# ── Pretty output ─────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; C=$'\033[36m'; N=$'\033[0m'
else
    B=""; G=""; Y=""; R=""; D=""; C=""; N=""
fi
say()  { echo "${B}→${N} $*"; }
ok()   { echo "${G}✓${N} $*"; }
warn() { echo "${Y}!${N} $*"; }
err()  { echo "${R}✗${N} $*" >&2; }
hr()   { echo "${D}────────────────────────────────────────${N}"; }

ask() {
    local prompt="$1" default="${2:-}" reply
    if [ -n "$default" ]; then
        read -r -p "${C}?${N} $prompt ${D}[$default]${N}: " reply < /dev/tty || true
        echo "${reply:-$default}"
    else
        read -r -p "${C}?${N} $prompt: " reply < /dev/tty || true
        echo "$reply"
    fi
}

ask_secret() {
    local prompt="$1" reply
    read -r -s -p "${C}?${N} $prompt ${D}(input hidden, blank to skip)${N}: " reply < /dev/tty || true
    echo >&2
    echo "$reply"
}

pause() {
    local prompt="${1:-Press Enter to continue}"
    read -r -p "${D}$prompt${N}" _ < /dev/tty || true
}

# ── OS detection ──────────────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM=macos ;;
    Linux)  PLATFORM=linux ;;
    *) err "Unsupported OS: $OS"; exit 1 ;;
esac

clear || true
echo "${B}╔══════════════════════════════════════════╗${N}"
echo "${B}║   Super-Bot-Telegram installer           ║${N}"
echo "${B}╚══════════════════════════════════════════╝${N}"
echo
echo "Repo:     $(pwd)"
echo "Platform: $PLATFORM"
echo

# ── Prerequisites ─────────────────────────────────────────────────────────────
say "${B}Checking prerequisites${N}"

have() { command -v "$1" > /dev/null 2>&1; }

if [ "$PLATFORM" = macos ] && ! have brew; then
    say "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" < /dev/tty
    # Add brew to PATH for the rest of this script
    if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
    if [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; fi
fi

install_pkg() {
    local pkg="$1" mac_name="${2:-$1}" apt_name="${3:-$1}"
    if have "$pkg"; then ok "$pkg ($(command -v "$pkg"))"; return; fi
    say "Installing $pkg..."
    if [ "$PLATFORM" = macos ]; then
        brew install "$mac_name"
    else
        sudo apt-get update -qq && sudo apt-get install -y "$apt_name"
    fi
}

install_pkg python3 python python3
install_pkg git
install_pkg ffmpeg
# curl is needed by most systems already, but check
have curl || install_pkg curl

# ── Step 1: Choose backend ────────────────────────────────────────────────────
echo
hr
say "${B}Step 1 — Choose AI backend${N}"
echo "  1) ${C}Claude Code${N}  — Anthropic's claude CLI (best agentic tools)"
echo "  2) ${C}Codex${N}        — OpenAI's codex CLI"
echo "  3) ${C}Ollama${N}       — local models on your machine (private, free, slower)"
echo
BACKEND_NUM="$(ask "Pick 1, 2, or 3" "1")"
case "$BACKEND_NUM" in
    1) BACKEND=claude ;;
    2) BACKEND=codex ;;
    3) BACKEND=ollama ;;
    *) err "Invalid choice"; exit 1 ;;
esac
ok "Selected backend: $BACKEND"

# ── Step 2: install backend, login, pick model ────────────────────────────────
echo
hr
say "${B}Step 2 — Install backend & pick model${N}"

ensure_node() {
    if have node; then return; fi
    say "Installing Node.js (needed for $1)..."
    if [ "$PLATFORM" = macos ]; then
        brew install node
    else
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
        sudo apt-get install -y nodejs
    fi
}

MODEL=""

case "$BACKEND" in
    claude)
        ensure_node "Claude Code"
        if ! have claude; then
            say "Installing Claude Code (@anthropic-ai/claude-code)..."
            npm install -g @anthropic-ai/claude-code
        fi
        ok "Claude Code: $(claude --version 2>/dev/null | head -1 || echo installed)"
        echo
        # Detect login state — Claude Code stores creds under ~/.claude or similar
        if [ -d "$HOME/.claude" ] && [ -n "$(ls -A "$HOME/.claude" 2>/dev/null)" ]; then
            ok "Claude Code looks already authenticated (~/.claude exists)"
        else
            warn "Claude Code is not authenticated yet."
            echo "  In a separate terminal run: ${B}claude${N}  (follow the browser login)"
            pause "Press Enter once login is done"
        fi
        echo
        say "Pick the default Claude model:"
        echo "  1) ${C}claude-opus-4-7[1m]${N}   — Opus 4.7 with 1M context (most capable)"
        echo "  2) ${C}sonnet${N}                — Sonnet 4.6 (fast & strong)"
        echo "  3) ${C}haiku${N}                 — Haiku 4.5 (fastest)"
        case "$(ask "Pick 1, 2, or 3" "1")" in
            1) MODEL="claude-opus-4-7[1m]" ;;
            2) MODEL="sonnet" ;;
            3) MODEL="haiku" ;;
            *) MODEL="claude-opus-4-7[1m]" ;;
        esac
        ok "Model: $MODEL"
        ;;

    codex)
        ensure_node "Codex"
        if ! have codex; then
            say "Installing Codex CLI (@openai/codex)..."
            npm install -g @openai/codex
        fi
        ok "Codex: $(codex --version 2>/dev/null | head -1 || echo installed)"
        echo
        if [ -d "$HOME/.codex" ] && [ -n "$(ls -A "$HOME/.codex" 2>/dev/null)" ]; then
            ok "Codex looks already authenticated (~/.codex exists)"
        else
            warn "Codex is not authenticated yet."
            echo "  In a separate terminal run: ${B}codex login${N}"
            pause "Press Enter once login is done"
        fi
        echo
        MODEL="$(ask "Codex model name (leave blank to let codex auto-pick)" "")"
        [ -n "$MODEL" ] && ok "Model: $MODEL" || ok "Model: (codex default)"
        ;;

    ollama)
        if ! have ollama; then
            say "Installing Ollama..."
            if [ "$PLATFORM" = macos ]; then
                brew install ollama
            else
                curl -fsSL https://ollama.com/install.sh | sh
            fi
        fi
        ok "Ollama: $(ollama --version 2>/dev/null | head -1 || echo installed)"

        # Make sure the server is reachable
        if ! curl -fsS http://localhost:11434/api/tags > /dev/null 2>&1; then
            say "Starting Ollama server..."
            if [ "$PLATFORM" = macos ]; then
                brew services start ollama 2>/dev/null || nohup ollama serve > /tmp/ollama.log 2>&1 &
            else
                # systemd unit ships with Ollama's Linux installer
                sudo systemctl enable --now ollama 2>/dev/null || nohup ollama serve > /tmp/ollama.log 2>&1 &
            fi
            for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
                if curl -fsS http://localhost:11434/api/tags > /dev/null 2>&1; then break; fi
                sleep 0.5
            done
        fi
        if curl -fsS http://localhost:11434/api/tags > /dev/null 2>&1; then
            ok "Ollama server reachable at http://localhost:11434"
        else
            err "Ollama server didn't start. Run \`ollama serve\` manually and re-run this installer."
            exit 1
        fi

        echo
        say "Installed Ollama models:"
        ollama list || true
        echo
        echo "${D}Browse models: https://ollama.com/library${N}"
        echo "${D}Examples: llama3.2, qwen2.5:14b, gpt-oss:20b, deepseek-r1:8b${N}"
        MODEL="$(ask "Model name (will pull if not present)" "llama3.2")"
        say "Pulling $MODEL (skipped if already present)..."
        ollama pull "$MODEL"
        ok "Model $MODEL ready"
        ;;
esac

# ── Step 3: Telegram setup ────────────────────────────────────────────────────
echo
hr
say "${B}Step 3 — Telegram bot setup${N}"
echo
echo "  ${D}Don't have a bot yet? Create one in 30 seconds:${N}"
echo "  ${D}  1. Open https://t.me/BotFather and send /newbot${N}"
echo "  ${D}  2. Pick a display name + a username ending in 'bot'${N}"
echo "  ${D}  3. Copy the token BotFather sends back${N}"
echo "  ${D}Everything else is automated via the Bot API.${N}"
echo

EXISTING_TOKEN=""
EXISTING_USER=""
if [ -f ".env" ]; then
    EXISTING_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' .env 2>/dev/null | cut -d= -f2- || true)"
    EXISTING_USER="$(grep -E '^ALLOWED_USER_ID=' .env 2>/dev/null | cut -d= -f2- || true)"
fi

ask_userid_manual() {
    local id=""
    while [ -z "$id" ] || ! [[ "$id" =~ ^-?[0-9]+$ ]]; do
        id="$(ask "Your Telegram user id (numeric — get it from https://t.me/userinfobot)")"
        if ! [[ "$id" =~ ^-?[0-9]+$ ]]; then
            warn "User id must be a number."
            id=""
        fi
    done
    echo "$id"
}

# Validate token (loops until accepted by Telegram) + auto-configure bot menu.
BOT_USERNAME=""
BOT_TOKEN=""
while [ -z "$BOT_USERNAME" ]; do
    while [ -z "$BOT_TOKEN" ]; do
        BOT_TOKEN="$(ask "Telegram bot token (123456:ABC-…)" "$EXISTING_TOKEN")"
    done
    say "Validating token & configuring bot via Telegram API..."
    if SETUP_OUT="$(python3 telegram-setup.py configure "$BOT_TOKEN" 2>&1)"; then
        BOT_USERNAME="$(echo "$SETUP_OUT" | grep '^username=' | cut -d= -f2-)"
        BOT_NAME="$(echo "$SETUP_OUT" | grep '^first_name=' | cut -d= -f2-)"
        ok "Connected as @${BOT_USERNAME} (${BOT_NAME:-bot}) — commands menu + description set"
    else
        err "Telegram API rejected this token:"
        echo "$SETUP_OUT" | grep -E '^(error|warn):' | head -5
        EXISTING_TOKEN=""
        BOT_TOKEN=""
    fi
done

# User id — keep existing, auto-detect, or type manually.
echo
say "${B}Your Telegram user id${N} (the bot only responds to you)"
USER_ID=""
if [ -n "$EXISTING_USER" ] && [[ "$EXISTING_USER" =~ ^-?[0-9]+$ ]]; then
    KEEP="$(ask "Existing id in .env is $EXISTING_USER — keep it? [Y/n]" "Y")"
    if [[ ! "$KEEP" =~ ^[Nn] ]]; then
        USER_ID="$EXISTING_USER"
        ok "Using existing user id: $USER_ID"
    fi
fi

if [ -z "$USER_ID" ]; then
    echo
    echo "  1) ${C}Auto-detect${N} — tap Start in @$BOT_USERNAME and we read your id from the API"
    echo "  2) ${C}Type manually${N} — get it from https://t.me/userinfobot first"
    case "$(ask "Pick 1 or 2" "1")" in
        2)
            USER_ID="$(ask_userid_manual)"
            ;;
        *)
            echo
            echo "  Open this in Telegram (we'll also try to launch it for you):"
            echo "    ${B}https://t.me/$BOT_USERNAME${N}"
            echo "  Then tap ${B}Start${N} (or send any message)."
            echo "  Waiting up to 3 minutes — Ctrl-C falls back to manual entry."
            if [ "$PLATFORM" = macos ] && command -v open > /dev/null 2>&1; then
                open "https://t.me/$BOT_USERNAME" 2>/dev/null || true
            elif [ "$PLATFORM" = linux ] && command -v xdg-open > /dev/null 2>&1; then
                xdg-open "https://t.me/$BOT_USERNAME" 2>/dev/null || true
            fi
            echo
            if DETECT_OUT="$(python3 telegram-setup.py detect-user "$BOT_TOKEN" 180 2>&1)"; then
                USER_ID="$(echo "$DETECT_OUT" | grep '^user_id=' | cut -d= -f2-)"
                FIRST="$(echo "$DETECT_OUT" | grep '^user_first_name=' | cut -d= -f2-)"
                UNAME="$(echo "$DETECT_OUT" | grep '^user_username=' | cut -d= -f2-)"
                LABEL="${FIRST:-user}"
                [ -n "$UNAME" ] && LABEL="$LABEL (@$UNAME)"
                ok "Detected: $LABEL — id $USER_ID"
            else
                warn "Auto-detect timed out — falling back to manual entry."
                USER_ID="$(ask_userid_manual)"
            fi
            ;;
    esac
fi

WORK_DIR="$(ask "Default working directory for AI commands" "$HOME")"

echo
echo "${D}Optional add-ons (press Enter to skip):${N}"
OPENAI_KEY="$(ask_secret "OpenAI API key (enables voice-message transcription)")"
XAI_KEY="$(ask_secret "xAI API key (enables TTS voice replies via Grok Ara)")"

# ── Step 4 (optional): Support-ticket investigation ───────────────────────────
echo
hr
say "${B}Step 4 (optional) — Support-ticket investigation${N}"
echo "  ${D}The bot opens a Telegram forum topic per ticket and runs Claude on${N}"
echo "  ${D}your codebase to diagnose it. Replies/resolutions are driven by webhooks${N}"
echo "  ${D}your web app POSTs to the bot — no shared DB needed. One bot can serve${N}"
echo "  ${D}multiple codebases (each ticket payload can override the project dir).${N}"
echo "  ${D}Supabase polling stays available as an OPTIONAL fallback for the${N}"
echo "  ${D}broker-marketplace setup.${N}"
echo

EXISTING_PROJECT_DIR=""
EXISTING_GROUP_ID=""
EXISTING_SUPABASE_URL=""
EXISTING_SUPABASE_KEY=""
EXISTING_WEBHOOK_TOKEN=""
EXISTING_WEBHOOK_PORT=""
EXISTING_WEBHOOK_BIND=""
if [ -f ".env" ]; then
    EXISTING_PROJECT_DIR="$(grep -E '^SUPPORT_PROJECT_DIR=' .env 2>/dev/null | cut -d= -f2-)"
    EXISTING_GROUP_ID="$(grep -E '^SUPPORT_GROUP_ID=' .env 2>/dev/null | cut -d= -f2-)"
    EXISTING_SUPABASE_URL="$(grep -E '^SUPABASE_URL=' .env 2>/dev/null | cut -d= -f2-)"
    EXISTING_SUPABASE_KEY="$(grep -E '^SUPABASE_SERVICE_ROLE_KEY=' .env 2>/dev/null | cut -d= -f2-)"
    EXISTING_WEBHOOK_TOKEN="$(grep -E '^SUPPORT_WEBHOOK_TOKEN=' .env 2>/dev/null | cut -d= -f2-)"
    EXISTING_WEBHOOK_PORT="$(grep -E '^SUPPORT_WEBHOOK_PORT=' .env 2>/dev/null | cut -d= -f2-)"
    EXISTING_WEBHOOK_BIND="$(grep -E '^SUPPORT_WEBHOOK_BIND=' .env 2>/dev/null | cut -d= -f2-)"
fi

SUPPORT_PROJECT_DIR=""
SUPPORT_GROUP_ID=""
SUPABASE_URL=""
SUPABASE_KEY=""
SUPPORT_WEBHOOK_TOKEN=""
SUPPORT_WEBHOOK_PORT=""
SUPPORT_WEBHOOK_BIND=""

DEFAULT_SUPPORT="N"
[ -n "$EXISTING_PROJECT_DIR" ] && DEFAULT_SUPPORT="Y"

ENABLE_SUPPORT="$(ask "Enable support-ticket investigation? [y/N]" "$DEFAULT_SUPPORT")"
if [[ "$ENABLE_SUPPORT" =~ ^[Yy] ]]; then
    SUPPORT_PROJECT_DIR="$(ask "Default codebase path (per-ticket payload can override)" "${EXISTING_PROJECT_DIR:-$HOME/code/my-app}")"

    echo
    echo "  ${D}Telegram group: the bot creates a forum topic per ticket here.${N}"
    echo "  ${D}Add the bot to a forum-enabled group, then in the group send /status${N}"
    echo "  ${D}to read the chat id (a negative number).${N}"
    SUPPORT_GROUP_ID="$(ask "Telegram support group id (negative number)" "$EXISTING_GROUP_ID")"

    echo
    echo "  ${B}Webhook listener${N} — your web app POSTs ticket events here."
    SUPPORT_WEBHOOK_PORT="$(ask "Local port" "${EXISTING_WEBHOOK_PORT:-9091}")"
    SUPPORT_WEBHOOK_BIND="$(ask "Bind address (127.0.0.1 = local-only, 0.0.0.0 = public)" "${EXISTING_WEBHOOK_BIND:-127.0.0.1}")"

    if [ -n "$EXISTING_WEBHOOK_TOKEN" ]; then
        REGEN="$(ask "Webhook token already exists in .env — regenerate? [y/N]" "N")"
        if [[ "$REGEN" =~ ^[Yy] ]]; then
            SUPPORT_WEBHOOK_TOKEN="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets;print(secrets.token_hex(32))')"
        else
            SUPPORT_WEBHOOK_TOKEN="$EXISTING_WEBHOOK_TOKEN"
        fi
    else
        SUPPORT_WEBHOOK_TOKEN="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets;print(secrets.token_hex(32))')"
    fi

    echo
    ENABLE_SUPABASE="$(ask "Also configure Supabase polling fallback? (only if your tickets are stored in a Supabase project the bot can read) [y/N]" "N")"
    if [[ "$ENABLE_SUPABASE" =~ ^[Yy] ]]; then
        SUPABASE_URL="$(ask "Supabase project URL (https://xxx.supabase.co)" "$EXISTING_SUPABASE_URL")"
        if [ -n "$EXISTING_SUPABASE_KEY" ]; then
            KEEP_KEY="$(ask "Reuse existing service-role key from .env? [Y/n]" "Y")"
            if [[ "$KEEP_KEY" =~ ^[Nn] ]]; then
                SUPABASE_KEY="$(ask_secret "Supabase service-role key")"
            else
                SUPABASE_KEY="$EXISTING_SUPABASE_KEY"
            fi
        else
            SUPABASE_KEY="$(ask_secret "Supabase service-role key")"
        fi
    fi

    BASE_URL="http://${SUPPORT_WEBHOOK_BIND}:${SUPPORT_WEBHOOK_PORT}"
    echo
    ok "Webhook base URL: ${B}${BASE_URL}${N}"
    ok "Webhook token:    ${B}${SUPPORT_WEBHOOK_TOKEN}${N}"
    echo
    echo "  ${B}Endpoints${N} (all expect ${D}Authorization: Bearer <token>${N}):"
    echo "    POST ${BASE_URL}/support-ticket           ${D}— new ticket → opens topic + investigates${N}"
    echo "    POST ${BASE_URL}/support-ticket/reply     ${D}— user replied → posted to topic${N}"
    echo "    POST ${BASE_URL}/support-ticket/resolved  ${D}— ticket closed → topic deleted${N}"
    echo
    echo "  ${D}Sample new-ticket call:${N}"
    cat <<EOM
    ${D}curl -X POST ${BASE_URL}/support-ticket \\
      -H 'Authorization: Bearer <token>' \\
      -H 'Content-Type: application/json' \\
      -d '{
        "id":           "<ticket-uuid>",
        "user_name":    "Alice",
        "user_email":   "alice@example.com",
        "message":      "Cannot upload PDFs over 10 MB",
        "current_page": "/upload",
        "attachments":  [{"url":"https://example.com/screenshot.png","filename":"screenshot.png"}],
        "project_dir":  "/path/to/this-codebase",
        "reply_url":    "https://your-app.com/webhooks/homi-reply",
        "reply_token":  "<your-own-shared-secret-for-Homi-replies>",
        "metadata":     {"plan":"pro","build":"abc123"}
      }'${N}
EOM
    if [[ "$SUPPORT_WEBHOOK_BIND" == "127.0.0.1" ]]; then
        echo
        warn "Webhook is bound to localhost — only reachable from THIS machine."
        echo "  ${D}If your web app runs elsewhere, either re-run install.sh with bind=0.0.0.0,${N}"
        echo "  ${D}or front the port with cloudflared / ngrok / tailscale-funnel.${N}"
    fi
fi

# ── Step 5: write .env ────────────────────────────────────────────────────────
echo
hr
say "${B}Step 5 — Writing config${N}"

# Preserve any existing keys we don't know about
KNOWN_KEYS='^(TELEGRAM_BOT_TOKEN|ALLOWED_USER_ID|CLAUDE_WORKING_DIR|BOT_BACKEND|CLAUDE_MODEL|CODEX_MODEL|OLLAMA_MODEL|OLLAMA_HOST|OPENAI_API_KEY|XAI_API_KEY|SUPPORT_PROJECT_DIR|SUPPORT_GROUP_ID|SUPABASE_URL|SUPABASE_SERVICE_ROLE_KEY|SUPPORT_WEBHOOK_TOKEN|SUPPORT_WEBHOOK_PORT|SUPPORT_WEBHOOK_BIND|#)='
PRESERVED=""
if [ -f ".env" ]; then
    PRESERVED="$(grep -vE "$KNOWN_KEYS" .env 2>/dev/null || true)"
fi

{
    echo "# Generated by install.sh on $(date)"
    echo "# Re-run ./install.sh to regenerate."
    echo
    echo "TELEGRAM_BOT_TOKEN=$BOT_TOKEN"
    echo "ALLOWED_USER_ID=$USER_ID"
    echo "CLAUDE_WORKING_DIR=$WORK_DIR"
    echo "BOT_BACKEND=$BACKEND"
    case "$BACKEND" in
        claude)  echo "CLAUDE_MODEL=$MODEL" ;;
        codex)   [ -n "$MODEL" ] && echo "CODEX_MODEL=$MODEL" ;;
        ollama)  echo "OLLAMA_MODEL=$MODEL"; echo "OLLAMA_HOST=http://localhost:11434" ;;
    esac
    [ -n "$OPENAI_KEY" ] && echo "OPENAI_API_KEY=$OPENAI_KEY"
    [ -n "$XAI_KEY" ]    && echo "XAI_API_KEY=$XAI_KEY"
    if [ -n "$SUPPORT_PROJECT_DIR" ]; then
        echo
        echo "# Support-ticket investigation"
        echo "SUPPORT_PROJECT_DIR=$SUPPORT_PROJECT_DIR"
        [ -n "$SUPPORT_GROUP_ID" ]      && echo "SUPPORT_GROUP_ID=$SUPPORT_GROUP_ID"
        [ -n "$SUPABASE_URL" ]          && echo "SUPABASE_URL=$SUPABASE_URL"
        [ -n "$SUPABASE_KEY" ]          && echo "SUPABASE_SERVICE_ROLE_KEY=$SUPABASE_KEY"
        [ -n "$SUPPORT_WEBHOOK_PORT" ]  && echo "SUPPORT_WEBHOOK_PORT=$SUPPORT_WEBHOOK_PORT"
        [ -n "$SUPPORT_WEBHOOK_BIND" ]  && echo "SUPPORT_WEBHOOK_BIND=$SUPPORT_WEBHOOK_BIND"
        [ -n "$SUPPORT_WEBHOOK_TOKEN" ] && echo "SUPPORT_WEBHOOK_TOKEN=$SUPPORT_WEBHOOK_TOKEN"
    fi
    if [ -n "$PRESERVED" ]; then
        echo
        echo "# Preserved from previous .env"
        echo "$PRESERVED"
    fi
} > .env
chmod 600 .env
ok ".env written (mode 600)"

# ── Step 6: Python deps in a venv ─────────────────────────────────────────────
say "Setting up Python venv & installing dependencies..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
ok "Python dependencies installed in ./venv"

chmod +x run-forever.sh

# ── Done ──────────────────────────────────────────────────────────────────────
echo
hr
echo "${G}${B}All set!${N}"
echo
echo "Start the bot now:"
echo "  ${B}cd $(pwd) && ./run-forever.sh${N}"
echo
echo "Run in the background (logs to bot.stdout.log / bot.stderr.log):"
echo "  ${B}cd $(pwd) && nohup ./run-forever.sh > bot.stdout.log 2> bot.stderr.log &${N}"
echo
echo "Reconfigure anytime:"
echo "  ${B}cd $(pwd) && ./install.sh${N}"
echo
START_NOW="$(ask "Start the bot in the foreground now? (Ctrl-C to stop) [y/N]" "n")"
if [[ "$START_NOW" =~ ^[Yy] ]]; then
    exec ./run-forever.sh
fi

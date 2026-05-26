#!/usr/bin/env python3
"""Telegram bot that bridges messages to Claude Code CLI with streaming updates."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from telegram import ReactionTypeEmoji, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegramify_markdown import markdownify

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WORKING_DIR = os.environ.get("CLAUDE_WORKING_DIR", os.path.expanduser("~"))
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

# Per-chat working directory overrides. Messages from these chats run claude/codex/term in the mapped dir.
# NOTE: When a regular group is converted to a supergroup, Telegram assigns a NEW chat_id. If you see
# "Group migrated to supergroup. New chat id: X" in the logs, replace the entry below with the new id.
CHAT_WORKING_DIRS: dict[int, str] = {
    -1003909732096: "/Users/russellpetty/Desktop/broker-marketplace",
}


def working_dir_for(chat_id: int) -> str:
    return CHAT_WORKING_DIRS.get(chat_id, WORKING_DIR)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_TTS_VOICE = os.environ.get("XAI_TTS_VOICE", "ara")
DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7[1m]")

HOMI_REPLY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "homi_reply.py")

# Ollama backend. When state.ollama_mode is True the bot still shells out to the
# `claude` CLI, but with ANTHROPIC_BASE_URL pointed at Ollama and --model set to
# OLLAMA_MODEL — so you get the full Claude Code agentic loop (tools, file edits,
# session resume) driven by a local model. Requires `ollama` >= 0.15 (exposes the
# Anthropic API on the same port) and a tool-capable model with a ≥64k context.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")

# Default backend for newly-seen chats: "claude" | "codex" | "ollama".
# Users can still switch at runtime with /codex, /ollama, /model.
DEFAULT_BACKEND = os.environ.get("BOT_BACKEND", "claude").lower()

# Support-ticket → Telegram topic dispatch (optional; skipped if env missing)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPPORT_GROUP_ID = int(os.environ["SUPPORT_GROUP_ID"]) if os.environ.get("SUPPORT_GROUP_ID") else None
SUPPORT_PROJECT_DIR = os.environ.get("SUPPORT_PROJECT_DIR", "/Users/russellpetty/Desktop/broker-marketplace")
SUPPORT_POLL_INTERVAL = int(os.environ.get("SUPPORT_POLL_INTERVAL", "20"))
SUPPORT_REPLY_POLL_INTERVAL = int(os.environ.get("SUPPORT_REPLY_POLL_INTERVAL", "30"))
SUPPORT_ARCHIVE_POLL_INTERVAL = int(os.environ.get("SUPPORT_ARCHIVE_POLL_INTERVAL", "60"))

# Webhook listener: web app POSTs a ticket JSON here for immediate dispatch
# (skipping the 20-second poll). Disabled if either var is empty.
SUPPORT_WEBHOOK_PORT = int(os.environ.get("SUPPORT_WEBHOOK_PORT", "0") or "0")
SUPPORT_WEBHOOK_TOKEN = os.environ.get("SUPPORT_WEBHOOK_TOKEN", "")
SUPPORT_WEBHOOK_BIND = os.environ.get("SUPPORT_WEBHOOK_BIND", "127.0.0.1")
HOMI_HEADSHOT_URL = "https://mortgagemarketplace.ai/Homi.png"
# Base URL for the broker-marketplace web app — used by homi_reply.py to hit the server-side
# endpoint that creates the Homi note AND sends the user the notification email.
SUPPORT_API_BASE_URL = os.environ.get("SUPPORT_API_BASE_URL", "https://mortgagemarketplace.ai").rstrip("/")

# Replies inside support-group forum topics should run claude in the project dir too.
if SUPPORT_GROUP_ID is not None:
    CHAT_WORKING_DIRS.setdefault(SUPPORT_GROUP_ID, SUPPORT_PROJECT_DIR)

TIMEOUT = 300  # 5 minutes
MAX_MSG_LEN = 4096  # Telegram message limit

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def is_allowed(update: Update) -> bool:
    """Return True if the message is from the allowed user."""
    user = update.effective_user
    if user and user.id == ALLOWED_USER_ID:
        return True
    logger.warning("Blocked message from user %s (id=%s)", user.username if user else "?", user.id if user else "?")
    return False


@dataclass
class ChatState:
    """Per-chat session state."""
    session_id: str | None = None
    model_override: str | None = None
    model_choices: list[str] = field(default_factory=list)
    active_proc: asyncio.subprocess.Process | None = None
    sent_message_ids: list[int] = field(default_factory=list)
    user_message_ids: list[int] = field(default_factory=list)
    term_mode: bool = False
    codex_mode: bool = False
    codex_thread_id: str | None = None
    codex_history: list[str] = field(default_factory=list)
    pending_codex_context: str | None = None
    ollama_mode: bool = False
    stop_requested: bool = False
    pending_text: list[str] = field(default_factory=list)
    debounce_task: asyncio.Task | None = None
    last_reply_to: object | None = None
    processing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    announce_next_session_id: bool = False


# (chat_id, thread_id) -> ChatState. thread_id is None for DMs and the General topic.
chats: dict[tuple[int, int | None], ChatState] = {}

# Reply poller bookkeeping. On first sight of a ticket we seed its note-ids as "seen" so we
# don't replay the full history; subsequent polls only fire on newly-added user notes.
_seen_ticket_notes: dict[str, set[str]] = {}
_first_sight_tickets: set[str] = set()


def get_state(chat_id: int, thread_id: int | None = None) -> ChatState:
    """Get or create state keyed by (chat_id, thread_id) so forum topics are isolated."""
    key = (chat_id, thread_id)
    if key not in chats:
        s = ChatState()
        if DEFAULT_BACKEND == "codex":
            s.codex_mode = True
        elif DEFAULT_BACKEND == "ollama":
            s.ollama_mode = True
        chats[key] = s
    return chats[key]


async def dispatch_to_backend(prompt, chat, reply_to, state, thread_id=None, voice_reply=False):
    """Route a prompt to whichever backend this chat is currently in.

    Ollama mode is handled inside run_claude_streaming (env vars + --model) — the
    bot only has two real runners now: claude (incl. claude-via-ollama) and codex.
    """
    if state.codex_mode:
        await run_codex_streaming(prompt, chat, reply_to, state, thread_id=thread_id, voice_reply=voice_reply)
    else:
        await run_claude_streaming(prompt, chat, reply_to, state, thread_id=thread_id, voice_reply=voice_reply)


def state_for(update: Update) -> ChatState:
    return get_state(update.message.chat_id, update.message.message_thread_id)


def _split_mdv2(text: str, limit: int) -> list[str]:
    """Split MarkdownV2 text into chunks at newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


async def send_chunks(chat, text: str, state: ChatState, thread_id: int | None = None) -> None:
    """Send text, converting Markdown to Telegram MarkdownV2 and splitting into chunks.

    thread_id is the forum topic's message_thread_id; pass None for DMs / General topic.
    """
    if not text:
        return
    try:
        converted = markdownify(text)
    except Exception as e:
        logger.warning("Markdown conversion failed, sending as plain text: %s", e)
        converted = None

    if converted:
        chunks = _split_mdv2(converted, MAX_MSG_LEN)
        for chunk in chunks:
            try:
                msg = await chat.send_message(chunk, parse_mode="MarkdownV2", message_thread_id=thread_id)
            except Exception as e:
                logger.warning("MarkdownV2 send failed, falling back to plain text: %s", e)
                msg = await chat.send_message(text[:MAX_MSG_LEN], parse_mode=None, message_thread_id=thread_id)
            state.sent_message_ids.append(msg.message_id)
    else:
        for i in range(0, len(text), MAX_MSG_LEN):
            msg = await chat.send_message(text[i : i + MAX_MSG_LEN], parse_mode=None, message_thread_id=thread_id)
            state.sent_message_ids.append(msg.message_id)


def format_tool_use(content_block: dict) -> str:
    """Format a tool_use block into a readable message."""
    name = content_block.get("name", "unknown")
    inp = content_block.get("input", {})

    if name == "Bash":
        desc = inp.get("description", "")
        if desc:
            return f"**{desc}**"
        return None
    elif name == "Write":
        path = inp.get("file_path", "")
        return f"📝 Writing file: `{path}`"
    elif name == "Edit":
        path = inp.get("file_path", "")
        return f"✏️ Editing file: `{path}`"
    elif name == "Read":
        path = inp.get("file_path", "")
        return f"📖 Reading file: `{path}`"
    elif name == "Glob":
        pattern = inp.get("pattern", "")
        return f"🔍 Searching for files: `{pattern}`"
    elif name == "Grep":
        pattern = inp.get("pattern", "")
        return f"🔍 Searching content: `{pattern}`"
    elif name == "WebFetch":
        url = inp.get("url", "")
        return f"🌐 Fetching: {url}"
    elif name == "WebSearch":
        query = inp.get("query", "")
        return f"🔎 Searching web: {query}"
    elif name == "Task":
        desc = inp.get("description", "")
        return f"🤖 Spawning agent: {desc}"
    else:
        return f"🔧 Using tool: {name}"


def format_tool_result(event: dict) -> str | None:
    """Format a tool result into a readable message, or None to skip."""
    content = event.get("message", {}).get("content", [])
    if not content:
        return None

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            inner = block.get("content", "")
            # content can be a string or a list of content blocks
            if isinstance(inner, str):
                text = inner
            elif isinstance(inner, list):
                parts = []
                for item in inner:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        parts.append(item.get("text", ""))
                text = "\n".join(parts)
            else:
                continue
            text = text.strip()
            if text:
                if len(text) > 2000:
                    text = text[:2000] + "\n... (truncated)"
                return f"📋 Result:\n```\n{text}\n```"
    return None


_MD_CODEBLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_BOLD_UL_RE = re.compile(r"__([^_]+)__")
_MD_ITALIC_UL_RE = re.compile(r"(?<!_)_([^_\n]+)_(?!_)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_HEADER_RE = re.compile(r"^\s*#{1,6}\s*", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)


def _strip_markdown_for_tts(text: str) -> str:
    """Strip basic markdown so TTS doesn't read formatting characters literally."""
    text = _MD_CODEBLOCK_RE.sub(r"\1", text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_BOLD_UL_RE.sub(r"\1", text)
    text = _MD_ITALIC_STAR_RE.sub(r"\1", text)
    text = _MD_ITALIC_UL_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_HEADER_RE.sub("", text)
    text = _MD_BULLET_RE.sub("", text)
    return text.strip()


async def send_tts_voice(chat, text: str, state: ChatState, thread_id: int | None = None) -> bool:
    """Speak `text` via Grok TTS (Ara) and send as a Telegram voice note.

    Falls back to `send_chunks` on any failure. Returns True iff a voice note was sent.
    """
    if not text.strip():
        return False
    if not XAI_API_KEY:
        logger.warning("XAI_API_KEY not set — sending text instead of voice")
        await send_chunks(chat, text, state, thread_id=thread_id)
        return False

    body = _strip_markdown_for_tts(text)
    if not body:
        return False
    # Grok TTS REST caps at 15000 chars; keep some headroom.
    if len(body) > 14500:
        body = body[:14500].rsplit(" ", 1)[0] + "…"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "https://api.x.ai/v1/tts",
                headers={
                    "Authorization": f"Bearer {XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"text": body, "voice_id": XAI_TTS_VOICE, "language": "en"},
            )
            resp.raise_for_status()
            mp3_bytes = resp.content
    except Exception as e:
        logger.error("Grok TTS request failed: %s — falling back to text", e)
        await send_chunks(chat, text, state, thread_id=thread_id)
        return False

    mp3_path = f"/tmp/tts_{uuid.uuid4().hex}.mp3"
    ogg_path = f"/tmp/tts_{uuid.uuid4().hex}.ogg"
    try:
        with open(mp3_path, "wb") as f:
            f.write(mp3_bytes)
        # Telegram only renders a true voice note (waveform) for OGG/OPUS.
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", mp3_path,
            "-c:a", "libopus", "-b:a", "64k", "-ar", "48000",
            ogg_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("ffmpeg conversion failed: %s", stderr.decode(errors="replace")[:500])
            await send_chunks(chat, text, state, thread_id=thread_id)
            return False
        with open(ogg_path, "rb") as f:
            msg = await chat.send_voice(voice=f, message_thread_id=thread_id)
            state.sent_message_ids.append(msg.message_id)
        return True
    except Exception as e:
        logger.error("Sending voice note failed: %s — falling back to text", e)
        await send_chunks(chat, text, state, thread_id=thread_id)
        return False
    finally:
        for p in (mp3_path, ogg_path):
            try:
                os.remove(p)
            except OSError:
                pass


async def run_claude_streaming(
    prompt: str,
    chat,
    reply_to,
    state: ChatState,
    thread_id: int | None = None,
    cwd_override: str | None = None,
    voice_reply: bool = False,
) -> None:
    """Run claude with stream-json output, sending updates as separate messages.

    thread_id scopes all sends to a forum topic. reply_to may be None for
    bot-initiated runs (e.g. support ticket dispatch) with no triggering message.
    """

    # Prepend codex context if switching back from codex
    if state.pending_codex_context:
        prompt = state.pending_codex_context + "\n\nUser's new message: " + prompt
        state.pending_codex_context = None

    # In ollama mode we still drive the `claude` CLI, but point it at the local
    # Ollama server (which now exposes the Anthropic API) and force the model to
    # the user's local Ollama model.
    if state.ollama_mode:
        model = state.model_override or OLLAMA_MODEL
    else:
        model = state.model_override or DEFAULT_MODEL

    cmd = [
        "claude", "-p", prompt,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
    ]
    if state.session_id:
        cmd.extend(["--resume", state.session_id])

    env = os.environ.copy()
    if state.ollama_mode:
        env["ANTHROPIC_BASE_URL"] = OLLAMA_HOST
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_API_KEY"] = ""  # explicit empty so any inherited real key doesn't override

    if reply_to is not None:
        try:
            await reply_to.set_reaction(ReactionTypeEmoji("👍"))
        except Exception as e:
            logger.error("Failed to set reaction: %s", e)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd_override or working_dir_for(chat.id),
            env=env,
            limit=10 * 1024 * 1024,  # 10 MB line limit for large JSON output
        )
        state.active_proc = proc
    except Exception as e:
        await chat.send_message(f"❌ Error starting Claude: {e}", message_thread_id=thread_id)
        return

    # Keep typing indicator alive in background
    typing_active = True

    async def keep_typing():
        while typing_active:
            try:
                await chat.send_action(ChatAction.TYPING, message_thread_id=thread_id)
            except Exception:
                pass
            await asyncio.sleep(8)

    typing_task = asyncio.create_task(keep_typing())

    try:
        buffer = ""
        async for raw_chunk in proc.stdout:
            buffer += raw_chunk.decode("utf-8", errors="replace")

            # Process complete JSON lines
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                try:
                    # Capture session ID from init or any event
                    if "session_id" in event and not state.session_id:
                        state.session_id = event["session_id"]
                        logger.info("Captured session_id: %s", state.session_id)
                        if state.announce_next_session_id:
                            state.announce_next_session_id = False
                            try:
                                announce = await chat.send_message(
                                    f"🆔 Session: `{state.session_id}`\nResume later with `claude --resume {state.session_id}`",
                                    message_thread_id=thread_id,
                                    parse_mode="Markdown",
                                )
                                state.sent_message_ids.append(announce.message_id)
                            except Exception as e:
                                logger.warning("Failed to announce session_id: %s", e)

                    if etype == "assistant":
                        msg = event.get("message", {})
                        content = msg.get("content", [])
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_use":
                                name = block.get("name", "")
                                if name in ("Bash", "WebSearch", "WebFetch"):
                                    summary = format_tool_use(block)
                                    if summary:
                                        await send_chunks(chat, summary, state, thread_id=thread_id)

                    elif etype == "result":
                        # Final response text — this is the only text we send
                        text = event.get("result", "").strip()
                        if text:
                            if voice_reply:
                                await send_tts_voice(chat, text, state, thread_id=thread_id)
                            else:
                                await send_chunks(chat, text, state, thread_id=thread_id)
                except Exception as e:
                    logger.warning("Error processing event: %s", e)
                    continue

        # Wait for process to finish
        await proc.wait()

        if proc.returncode != 0 and not state.stop_requested:
            stderr = await proc.stderr.read()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                await send_chunks(chat, f"⚠️ Claude exited with errors:\n{stderr_text[:3000]}", state, thread_id=thread_id)

    except asyncio.TimeoutError:
        await chat.send_message("⏰ Claude timed out after 5 minutes.", message_thread_id=thread_id)
        proc.kill()
    except Exception as e:
        await chat.send_message(f"❌ Error: {e}", message_thread_id=thread_id)
    finally:
        state.active_proc = None
        state.stop_requested = False
        typing_active = False
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


async def run_codex_streaming(
    prompt: str,
    chat,
    reply_to,
    state: ChatState,
    thread_id: int | None = None,
    voice_reply: bool = False,
) -> None:
    """Run codex exec with --json output, sending updates as separate messages."""

    cwd = working_dir_for(chat.id)
    cmd = ["codex", "exec"]
    # Resume the same Codex session so multi-turn context carries across messages.
    # `codex exec resume` does not accept -C; the subprocess cwd below sets the
    # working directory for both paths, and codex filters resumable sessions by cwd.
    if state.codex_thread_id:
        cmd.append("resume")
    cmd.extend([
        "--dangerously-bypass-approvals-and-sandbox",
        "--json",
    ])
    if not state.codex_thread_id:
        cmd.extend(["-C", cwd])
    if state.model_override:
        cmd.extend(["-m", state.model_override])
    if state.codex_thread_id:
        cmd.append(state.codex_thread_id)
    cmd.append(prompt)

    if reply_to is not None:
        try:
            await reply_to.set_reaction(ReactionTypeEmoji("👍"))
        except Exception as e:
            logger.error("Failed to set reaction: %s", e)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=10 * 1024 * 1024,
        )
        state.active_proc = proc
    except Exception as e:
        await chat.send_message(f"❌ Error starting Codex: {e}", message_thread_id=thread_id)
        return

    # Close stdin so codex doesn't hang waiting for input
    proc.stdin.close()

    # Track user prompt in codex history
    state.codex_history.append(f"User: {prompt}")

    # Keep typing indicator alive in background
    typing_active = True

    async def keep_typing():
        while typing_active:
            try:
                await chat.send_action(ChatAction.TYPING, message_thread_id=thread_id)
            except Exception:
                pass
            await asyncio.sleep(8)

    typing_task = asyncio.create_task(keep_typing())

    try:
        buffer = ""
        async for raw_chunk in proc.stdout:
            buffer += raw_chunk.decode("utf-8", errors="replace")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                try:
                    if etype == "thread.started":
                        tid = event.get("thread_id")
                        if tid:
                            state.codex_thread_id = tid
                            logger.info("Codex thread_id: %s", tid)

                    elif etype == "item.completed":
                        item = event.get("item", {})
                        itype = item.get("type", "")

                        if itype == "agent_message":
                            text = item.get("text", "").strip()
                            if text:
                                if voice_reply:
                                    await send_tts_voice(chat, text, state, thread_id=thread_id)
                                else:
                                    await send_chunks(chat, text, state, thread_id=thread_id)
                                state.codex_history.append(f"Codex: {text}")

                        elif itype == "command_execution":
                            cmd_str = item.get("command", "")
                            exit_code = item.get("exit_code")
                            status = item.get("status", "")
                            if cmd_str and status == "completed":
                                icon = "✅" if exit_code == 0 else "⚠️"
                                await send_chunks(chat, f"{icon} `{cmd_str}` (exit {exit_code})", state, thread_id=thread_id)

                except Exception as e:
                    logger.warning("Error processing codex event: %s", e)
                    continue

        await proc.wait()

        if proc.returncode != 0 and not state.stop_requested:
            stderr = await proc.stderr.read()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                await send_chunks(chat, f"⚠️ Codex exited with errors:\n{stderr_text[:3000]}", state, thread_id=thread_id)

    except Exception as e:
        await chat.send_message(f"❌ Error: {e}", message_thread_id=thread_id)
    finally:
        state.active_proc = None
        state.stop_requested = False
        typing_active = False
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


async def run_terminal_command(command: str, chat, reply_to, state: ChatState, thread_id: int | None = None) -> None:
    """Run a shell command and relay output back to the chat."""
    working_msg = await reply_to.reply_text(f"🖥️ Running: `{command}`", parse_mode="Markdown")
    state.sent_message_ids.append(working_msg.message_id)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=working_dir_for(chat.id),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
        output = stdout.decode("utf-8", errors="replace").strip()

        if not output:
            output = "(no output)"

        exit_info = f"Exit code: {proc.returncode}"
        result = f"```\n{output}\n```\n{exit_info}"
        await send_chunks(chat, result, state, thread_id=thread_id)

    except asyncio.TimeoutError:
        await send_chunks(chat, "⏰ Command timed out after 5 minutes.", state, thread_id=thread_id)
        proc.kill()
    except Exception as e:
        await send_chunks(chat, f"❌ Error: {e}", state, thread_id=thread_id)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start."""
    if not is_allowed(update):
        return
    state = state_for(update)
    msg = await update.message.reply_text(
        "Hello! I'm a bridge to Claude Code. Send me a message and I'll forward it "
        "to Claude. Use /new to start a fresh session, or /stop to stop what's running."
    )
    state.sent_message_ids.append(msg.message_id)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new — reset session and clear all messages from chat."""
    if not is_allowed(update):
        return
    state = state_for(update)
    state.session_id = None
    state.model_override = None
    state.codex_mode = (DEFAULT_BACKEND == "codex")
    state.codex_thread_id = None
    state.codex_history = []
    state.pending_codex_context = None
    state.ollama_mode = (DEFAULT_BACKEND == "ollama")
    state.announce_next_session_id = True

    chat_id = update.message.chat_id
    all_ids = state.sent_message_ids + state.user_message_ids
    for msg_id in all_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  # Message may already be deleted or too old (>48h)
    state.sent_message_ids = []
    state.user_message_ids = []

    # Delete the /new command message itself
    try:
        await update.message.delete()
    except Exception:
        pass

    await update.message.reply_text("Hi, how can I help you?")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop — kill the running Claude process without resetting the session."""
    if not is_allowed(update):
        return
    state = state_for(update)

    # Cancel any pending debounce so queued chunks don't fire after stop
    if state.debounce_task and not state.debounce_task.done():
        state.debounce_task.cancel()
        state.debounce_task = None
    state.pending_text.clear()

    if state.active_proc is None:
        msg = await update.message.reply_text("Nothing is running right now.")
        state.sent_message_ids.append(msg.message_id)
        return
    state.stop_requested = True
    try:
        state.active_proc.kill()
    except ProcessLookupError:
        pass
    msg = await update.message.reply_text("🛑 Stopped.")
    state.sent_message_ids.append(msg.message_id)


async def term_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /term — next message will be run as a shell command."""
    if not is_allowed(update):
        return
    state = state_for(update)
    state.term_mode = True
    msg = await update.message.reply_text("🖥️ Term mode active. Send a command to run.")
    state.sent_message_ids.append(msg.message_id)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — show current session state."""
    if not is_allowed(update):
        return
    state = state_for(update)
    running = "Yes" if state.active_proc is not None else "No"
    session = f"Active (`{state.session_id[:8]}...`)" if state.session_id else "Fresh (next message starts new)"
    if state.term_mode:
        mode = "Terminal"
    elif state.ollama_mode:
        mode = "Ollama"
    elif state.codex_mode:
        mode = "Codex"
    else:
        mode = "Claude"
    thread_id = update.message.message_thread_id

    lines = [
        f"Chat ID: `{update.message.chat_id}`",
        f"Thread ID: `{thread_id}`",
        f"Working dir: `{working_dir_for(update.message.chat_id)}`",
        f"Session: {session}",
        f"Process running: {running}",
        f"Input mode: {mode}",
    ]
    msg = await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    state.sent_message_ids.append(msg.message_id)


async def _list_ollama_models() -> list[str]:
    """Query the local Ollama server for installed models."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_HOST}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception as e:
        logger.warning("Failed to list ollama models: %s", e)
        return []


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model — list available models for the current backend and let user pick one."""
    if not is_allowed(update):
        return
    state = state_for(update)

    if state.ollama_mode:
        models = await _list_ollama_models()
        if not models:
            models = [OLLAMA_MODEL]
        header = "Ollama"
    else:
        # /model in codex mode swaps back to Claude (preserves prior behavior).
        if state.codex_mode:
            if state.codex_history:
                transcript = "\n".join(state.codex_history)
                state.pending_codex_context = (
                    "The user was previously working with Codex. Here is the conversation that took place:\n\n"
                    f"{transcript}\n\n"
                    "Continue assisting them, taking the above context into account."
                )
            state.codex_mode = False
            state.codex_thread_id = None
            state.codex_history = []
        models = ["claude-opus-4-7[1m]", "sonnet", "haiku"]
        header = "Claude"

    state.model_choices = models

    current = state.model_override or "default"
    lines = [f"**{header} — current model:** `{current}`\n", "**Pick a model** (reply with the number):\n"]
    for i, m in enumerate(models, 1):
        check = " ✅" if m == state.model_override else ""
        lines.append(f"`{i}.` {m}{check}")
    lines.append(f"\n`0.` Reset to default")

    msg = await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    state.sent_message_ids.append(msg.message_id)


async def codex_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /codex — switch to Codex mode, seeding it with all prior chat context."""
    if not is_allowed(update):
        return
    state = state_for(update)
    thread_id = update.message.message_thread_id

    if state.codex_mode:
        msg = await update.message.reply_text("Already in Codex mode. Use /new or /model to switch back to Claude.")
        state.sent_message_ids.append(msg.message_id)
        return

    # Collect all message history from this chat to seed Codex
    chat_obj = update.message.chat
    history_lines = []
    try:
        # Gather recent messages from Telegram chat history
        # We go through sent + user message IDs we've tracked
        # But more reliably, we can use the bot's tracked context
        pass
    except Exception:
        pass

    # Build context from what we know: replay any session context
    # The real value is forwarding the conversation so far
    # Collect messages by iterating tracked user messages
    # Since we can't easily read back message text from IDs alone,
    # we'll note the session switch and let the user continue from here
    state.codex_mode = True
    state.ollama_mode = False
    state.codex_thread_id = None  # Fresh codex session

    msg = await update.message.reply_text(
        "🔄 Switched to **Codex** mode (`--dangerously-bypass-approvals-and-sandbox`).\n\n"
        "All messages will now be routed to Codex.\n"
        "Use /new or /model to switch back to Claude.",
        parse_mode="Markdown",
    )
    state.sent_message_ids.append(msg.message_id)

    # If there's an existing Claude session, build a context summary prompt
    # and send it to Codex as the first message so it has the conversation context
    if state.session_id:
        context_prompt = (
            "You are continuing a conversation that was previously handled by Claude Code. "
            "The user has switched to Codex. Continue assisting them with whatever they need. "
            "The previous Claude session ID was: " + state.session_id
        )
        async with state.processing_lock:
            await run_codex_streaming(context_prompt, chat_obj, update.message, state, thread_id=thread_id)


async def ollama_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ollama — switch to Ollama mode (local model via http://localhost:11434)."""
    if not is_allowed(update):
        return
    state = state_for(update)

    if state.ollama_mode:
        msg = await update.message.reply_text(
            f"Already in Ollama mode (model: `{state.model_override or OLLAMA_MODEL}`). Use /model to pick a different one.",
            parse_mode="Markdown",
        )
        state.sent_message_ids.append(msg.message_id)
        return

    # Confirm the Ollama server is up before flipping the mode.
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{OLLAMA_HOST}/api/tags")
            resp.raise_for_status()
    except Exception as e:
        msg = await update.message.reply_text(
            f"❌ Can't reach Ollama at `{OLLAMA_HOST}` ({e}). Run `ollama serve` or `brew services start ollama`.",
            parse_mode="Markdown",
        )
        state.sent_message_ids.append(msg.message_id)
        return

    state.ollama_mode = True
    state.codex_mode = False
    state.session_id = None  # any prior Anthropic-cloud session won't resume against Ollama

    msg = await update.message.reply_text(
        f"🦙 Switched to **Ollama** mode (model: `{state.model_override or OLLAMA_MODEL}` via Claude Code).\n\n"
        f"You get the full Claude Code agentic loop — tools, file edits, session resume — "
        f"but the model running it is local. Use /model to pick a different installed model, "
        f"or /new to swap back to the default backend.",
        parse_mode="Markdown",
    )
    state.sent_message_ids.append(msg.message_id)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages — transcribe with OpenAI Whisper, send to Claude."""
    if not is_allowed(update):
        return
    state = state_for(update)
    thread_id = update.message.message_thread_id
    state.user_message_ids.append(update.message.message_id)

    if not OPENAI_API_KEY:
        msg = await update.message.reply_text("⚠️ OPENAI_API_KEY not set — can't transcribe voice.")
        state.sent_message_ids.append(msg.message_id)
        return

    voice = update.message.voice
    file = await voice.get_file()
    voice_path = f"/tmp/tg_voice_{uuid.uuid4().hex}.ogg"
    await file.download_to_drive(voice_path)
    logger.info("Saved voice message to %s (%d seconds)", voice_path, voice.duration)

    # Transcribe via OpenAI Whisper API
    try:
        import urllib.request
        import urllib.error

        with open(voice_path, "rb") as audio_file:
            audio_data = audio_file.read()

        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="voice.ogg"\r\n'
            f"Content-Type: audio/ogg\r\n\r\n"
        ).encode() + audio_data + (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"whisper-1"
            f"\r\n--{boundary}--\r\n"
        ).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            transcript = result.get("text", "").strip()
    except Exception as e:
        logger.error("Whisper transcription failed: %s", e)
        msg = await update.message.reply_text(f"⚠️ Transcription failed: {e}")
        state.sent_message_ids.append(msg.message_id)
        return
    finally:
        try:
            os.remove(voice_path)
        except OSError:
            pass

    if not transcript:
        msg = await update.message.reply_text("Couldn't transcribe the voice message.")
        state.sent_message_ids.append(msg.message_id)
        return

    logger.info("Transcribed voice: %s", transcript[:100])
    await dispatch_to_backend(transcript, update.message.chat, update.message, state, thread_id=thread_id, voice_reply=True)


async def _process_debounced(chat, state: ChatState, thread_id: int | None) -> None:
    """Wait for the debounce window, then send all buffered text to Claude."""
    await asyncio.sleep(1.5)  # Debounce window — wait for more chunks

    # Grab everything that accumulated and clear the buffer
    combined = "\n".join(state.pending_text)
    reply_to = state.last_reply_to
    state.pending_text = []
    state.last_reply_to = None

    if not combined.strip():
        return

    async with state.processing_lock:
        await dispatch_to_backend(combined, chat, reply_to, state, thread_id=thread_id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages with debounce for chunked inputs."""
    if not is_allowed(update):
        return
    state = state_for(update)
    thread_id = update.message.message_thread_id
    text = update.message.text
    if not text:
        return
    state.user_message_ids.append(update.message.message_id)

    # Handle model selection if choices are pending
    if state.model_choices and text.strip().isdigit():
        idx = int(text.strip())
        if idx == 0:
            state.model_override = None
            state.model_choices = []
            msg = await update.message.reply_text("Model reset to default.")
            state.sent_message_ids.append(msg.message_id)
            return
        if 1 <= idx <= len(state.model_choices):
            state.model_override = state.model_choices[idx - 1]
            state.model_choices = []
            msg = await update.message.reply_text(f"Model set to `{state.model_override}`", parse_mode="Markdown")
            state.sent_message_ids.append(msg.message_id)
            return
        state.model_choices = []  # Invalid number, clear and fall through

    if state.term_mode:
        state.term_mode = False
        await run_terminal_command(text, update.message.chat, update.message, state, thread_id=thread_id)
        return

    # Buffer the message and (re)start the debounce timer
    state.pending_text.append(text)
    state.last_reply_to = update.message

    if state.debounce_task and not state.debounce_task.done():
        state.debounce_task.cancel()

    state.debounce_task = asyncio.create_task(
        _process_debounced(update.message.chat, state, thread_id)
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages — save image, ask Claude to read it."""
    if not is_allowed(update):
        return
    state = state_for(update)
    thread_id = update.message.message_thread_id
    state.user_message_ids.append(update.message.message_id)
    photo = update.message.photo[-1]
    file = await photo.get_file()

    img_path = f"/tmp/tg_img_{uuid.uuid4().hex}.jpg"
    await file.download_to_drive(img_path)
    logger.info("Saved image to %s", img_path)

    caption = update.message.caption or ""
    if caption:
        prompt = f"Read the image at {img_path}. User says: {caption}"
    else:
        prompt = f"Read the image at {img_path} and describe what you see."

    await dispatch_to_backend(prompt, update.message.chat, update.message, state, thread_id=thread_id)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document messages — save file, ask Claude to read it."""
    if not is_allowed(update):
        return
    state = state_for(update)
    thread_id = update.message.message_thread_id
    state.user_message_ids.append(update.message.message_id)
    doc = update.message.document
    file = await doc.get_file()

    filename = doc.file_name or f"document_{uuid.uuid4().hex}"
    doc_path = f"/tmp/tg_doc_{uuid.uuid4().hex}_{filename}"
    await file.download_to_drive(doc_path)
    logger.info("Saved document to %s (%s, %d bytes)", doc_path, doc.mime_type, doc.file_size or 0)

    caption = update.message.caption or ""
    if caption:
        prompt = f"Read the file at {doc_path}. User says: {caption}"
    else:
        prompt = f"Read the file at {doc_path} and describe its contents."

    await dispatch_to_backend(prompt, update.message.chat, update.message, state, thread_id=thread_id)


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


async def attach_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /attach <session_id> — attach an existing Claude session to this chat/topic so
    the next message resumes it via `claude --resume <id>`."""
    if not is_allowed(update):
        return
    state = state_for(update)
    text = update.message.text or ""
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Usage: /attach <claude-session-id>")
        return
    session_id = parts[1].strip()
    if not _UUID_RE.match(session_id):
        await update.message.reply_text(
            "⚠️ session_id should be a UUID (e.g. 01234567-89ab-cdef-0123-456789abcdef)."
        )
        return
    state.session_id = session_id
    state.announce_next_session_id = False
    msg = await update.message.reply_text(
        f"🔗 Attached Claude session `{session_id}` — next message will resume it.",
        parse_mode="Markdown",
    )
    state.sent_message_ids.append(msg.message_id)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart — restart the entire bot process."""
    if not is_allowed(update):
        return
    await update.message.reply_text("🔄 Restarting bot...")
    os._exit(0)


# -----------------------------------------------------------------------------
# Support ticket → forum topic dispatch
# -----------------------------------------------------------------------------


def _supabase_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def _fetch_pending_tickets(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/support_tickets",
        params={
            "select": "id,user_id,user_name,user_email,message,ticket_type,current_page,device_type,created_at,attachments",
            "telegram_dispatched_at": "is.null",
            "status": "eq.open",
            "order": "created_at.asc",
            "limit": "5",
        },
        headers=_supabase_headers(),
    )
    resp.raise_for_status()
    return resp.json()


IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}


async def _download_attachment(client: httpx.AsyncClient, storage_path: str, filename: str) -> str | None:
    """Sign and download a ticket attachment from the support_tickets bucket to /tmp."""
    try:
        sign_resp = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/sign/support_tickets/{storage_path}",
            json={"expiresIn": 300},
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
        )
        if sign_resp.status_code >= 400:
            logger.warning("sign URL failed for %s: %s", storage_path, sign_resp.text[:200])
            return None
        signed_url = f"{SUPABASE_URL}/storage/v1{sign_resp.json()['signedURL']}"
        dl_resp = await client.get(signed_url)
        if dl_resp.status_code >= 400:
            logger.warning("download failed for %s: %s", storage_path, dl_resp.status_code)
            return None
        safe_name = "".join(c for c in filename if c.isalnum() or c in "._-")[:60] or "attachment"
        local_path = f"/tmp/ticket_att_{uuid.uuid4().hex}_{safe_name}"
        with open(local_path, "wb") as f:
            f.write(dl_resp.content)
        return local_path
    except Exception as e:
        logger.error("attachment download error (%s): %s", storage_path, e)
        return None


async def _download_ticket_attachments(client: httpx.AsyncClient, attachments: list[dict]) -> list[str]:
    paths: list[str] = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        storage_path = att.get("storagePath")
        name = att.get("name") or "attachment"
        if not storage_path:
            continue
        local = await _download_attachment(client, storage_path, name)
        if local:
            paths.append(local)
    return paths


async def _send_attachment_to_topic(bot, thread_id: int, path: str) -> None:
    """Post a downloaded attachment into the forum topic — image as photo, else document."""
    ext = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    try:
        if ext in IMAGE_EXTS:
            with open(path, "rb") as f:
                await bot.send_photo(chat_id=SUPPORT_GROUP_ID, message_thread_id=thread_id, photo=f)
        else:
            with open(path, "rb") as f:
                await bot.send_document(chat_id=SUPPORT_GROUP_ID, message_thread_id=thread_id, document=f)
    except Exception as e:
        logger.error("Failed to send attachment %s to topic %s: %s", path, thread_id, e)


async def _mark_ticket_dispatched(client: httpx.AsyncClient, ticket_id: str, topic_id: int) -> None:
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/support_tickets",
        params={"id": f"eq.{ticket_id}"},
        json={
            "telegram_topic_id": topic_id,
            "telegram_dispatched_at": datetime.now(timezone.utc).isoformat(),
        },
        headers=_supabase_headers(),
    )
    resp.raise_for_status()


async def _dispatch_ticket(context: ContextTypes.DEFAULT_TYPE, client: httpx.AsyncClient, ticket: dict) -> None:
    bot = context.bot
    ticket_id = ticket["id"]
    user_name = ticket.get("user_name") or "Unknown"
    first_line = (ticket.get("message") or "").strip().split("\n")[0]
    snippet = first_line[:40] + ("…" if len(first_line) > 40 else "")
    topic_name = f"{user_name[:20]} — {snippet}" if snippet else f"Ticket {ticket_id[:8]}"

    try:
        topic = await bot.create_forum_topic(chat_id=SUPPORT_GROUP_ID, name=topic_name)
    except Exception as e:
        logger.error("create_forum_topic failed for %s: %s", ticket_id, e)
        return

    thread_id = topic.message_thread_id
    logger.info("Created topic %s (thread_id=%s) for ticket %s", topic_name, thread_id, ticket_id)

    intro = (
        f"*New support ticket*\n\n"
        f"*User:* {user_name} (`{ticket.get('user_id','?')}`)\n"
        f"*Email:* {ticket.get('user_email','?')}\n"
        f"*Type:* {ticket.get('ticket_type','?')}\n"
        f"*Page:* `{ticket.get('current_page') or '(unknown)'}`\n"
        f"*Device:* {ticket.get('device_type') or '(unknown)'}\n"
        f"*Ticket ID:* `{ticket_id}`\n\n"
        f"*Message:*\n{ticket.get('message') or ''}"
    )
    try:
        await bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=thread_id,
            text=markdownify(intro),
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error("Posting intro to topic %s failed: %s", thread_id, e)

    # Download + post ticket attachments (screenshots, PDFs, etc.) so both Claude and the human reviewer see them.
    attachment_paths = await _download_ticket_attachments(client, ticket.get("attachments") or [])
    for path in attachment_paths:
        await _send_attachment_to_topic(bot, thread_id, path)

    # Pre-seed the reply poller for this ticket so we don't replay any pre-existing notes on first sight.
    _first_sight_tickets.add(ticket_id)
    _seen_ticket_notes.setdefault(ticket_id, set())

    # Mark dispatched BEFORE Claude runs so the poller can move on and crashes don't trigger duplicate topics.
    try:
        await _mark_ticket_dispatched(client, ticket_id, thread_id)
    except Exception as e:
        logger.error("Failed to mark ticket %s dispatched: %s", ticket_id, e)

    # Fire Claude investigation as a background task so it doesn't block subsequent ticket dispatches.
    asyncio.create_task(_investigate_ticket(bot, ticket, thread_id, attachment_paths))


async def _investigate_ticket(bot, ticket: dict, thread_id: int, attachment_paths: list[str] | None = None) -> None:
    """Run Claude on a freshly-dispatched ticket in its forum topic."""
    ticket_id = ticket["id"]
    user_name = ticket.get("user_name") or "Unknown"
    att_block = ""
    if attachment_paths:
        att_block = (
            "\n\nAttachments the user uploaded with this ticket (read them — screenshots often show the error directly):\n"
            + "\n".join(f"- {p}" for p in attachment_paths)
        )
    investigation_prompt = (
        f"A user opened support ticket {ticket_id}. Investigate it.\n\n"
        f"User: {user_name} (id: {ticket.get('user_id','?')}, email: {ticket.get('user_email','?')})\n"
        f"Page they were on: {ticket.get('current_page') or '(unknown)'}\n"
        f"Device: {ticket.get('device_type') or '(unknown)'}\n"
        f"Type: {ticket.get('ticket_type','?')}\n\n"
        f"User's message:\n{ticket.get('message') or ''}"
        f"{att_block}\n\n"
        f"Investigation steps:\n"
        f"1. Query the Supabase `support_tickets` table via the Supabase MCP tools for PRIOR tickets related to this issue — "
        f"search by similar `message` text, same `current_page`, same `ticket_type`, or same error symptoms. "
        f"Read their `notes` arrays to see how similar problems were diagnosed and resolved; surface any recurring patterns.\n"
        f"2. Pull recent production logs from Railway to find the user's actual server-side error. The Railway CLI is "
        f"authenticated on this host and the `broker-marketplace`/`production` project is already linked from "
        f"`{SUPPORT_PROJECT_DIR}`, so a plain `railway logs ...` invocation resolves correctly. Run:\n"
        f"   `railway logs --service 113a809f-dd5d-4c08-915d-dd0052ad964d --lines 500`\n"
        f"   That service UUID is the `Production!` Next.js app container (always pass the UUID — the `!` in the name "
        f"breaks CLI resolution). Grep the output for the user's id (`{ticket.get('user_id','?')}`), their email, "
        f"`{ticket.get('user_email','?')}`, or the page they were on. Useful flags: `--build` for build logs, "
        f"`--http` for request logs, `--since 1h` (or `30m`, `1d`) to time-scope, `--json` for structured filtering. "
        f"If the issue might be in a different service (Worker, Temporal Worker, marketplace-affiliate, etc.), "
        f"run `railway service status --json --all` to list all service UUIDs.\n"
        f"3. Investigate the codebase at {SUPPORT_PROJECT_DIR} and any relevant Supabase data.\n"
        f"4. If you need more information from the user to diagnose the issue, DRAFT a reply asking the specific question. "
        f"Show me the draft in this thread and WAIT for me to approve it (e.g. \"yes\", \"send it\", \"go\"). "
        f"Once I approve, send it as Homi by piping the body into:\n"
        f"   `python3 {HOMI_REPLY_PATH} --ticket-id {ticket_id}`\n"
        f"   (Use a heredoc for multi-line bodies. The tool POSTs through the web app so the user gets the normal email.)\n"
        f"   Exception: if the only reply needed is a pure thank-you / acknowledgment with no new information, "
        f"send it directly without asking for approval.\n"
        f"5. Identify the likely root cause and propose a specific fix.\n\n"
        f"DO NOT modify any code yet — wait for my reply in this thread before making changes."
    )

    state = get_state(SUPPORT_GROUP_ID, thread_id)
    state.session_id = None  # fresh Claude session per ticket

    try:
        chat_obj = await bot.get_chat(SUPPORT_GROUP_ID)
        async with state.processing_lock:
            await run_claude_streaming(
                investigation_prompt,
                chat_obj,
                reply_to=None,
                state=state,
                thread_id=thread_id,
                cwd_override=SUPPORT_PROJECT_DIR,
            )
    except Exception as e:
        logger.error("Investigation failed for ticket %s: %s", ticket_id, e, exc_info=True)


async def poll_tickets(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: pull undispatched support tickets and open a topic for each."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and SUPPORT_GROUP_ID):
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            tickets = await _fetch_pending_tickets(client)
            if not tickets:
                return
            logger.info("Dispatching %d pending ticket(s)", len(tickets))
            for ticket in tickets:
                try:
                    await _dispatch_ticket(context, client, ticket)
                except Exception as e:
                    logger.error("Dispatch failed for ticket %s: %s", ticket.get("id"), e, exc_info=True)
    except Exception as e:
        logger.error("poll_tickets error: %s", e, exc_info=True)


async def _fetch_active_dispatched_tickets(client: httpx.AsyncClient) -> list[dict]:
    """Tickets that already have a Telegram topic and are still open — candidates for reply forwarding."""
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/support_tickets",
        params={
            "select": "id,user_id,user_name,user_email,current_page,telegram_topic_id,notes,message",
            "telegram_topic_id": "not.is.null",
            "status": "eq.open",
        },
        headers=_supabase_headers(),
    )
    resp.raise_for_status()
    return resp.json()


async def _handle_new_user_note(bot, client: httpx.AsyncClient, ticket: dict, thread_id: int, note: dict) -> None:
    """Post a user reply into the existing forum thread and resume Claude's session with the new context."""
    ticket_id = ticket["id"]
    user_name = note.get("author_name") or ticket.get("user_name") or "User"
    content = (note.get("content") or "").strip()

    header = f"*Reply from {user_name}:*\n\n{content}" if content else f"*Reply from {user_name}:* (no text)"
    try:
        await bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=thread_id,
            text=markdownify(header),
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error("Posting user reply to topic %s failed: %s", thread_id, e)

    local_paths = await _download_ticket_attachments(client, note.get("attachments") or [])
    for path in local_paths:
        await _send_attachment_to_topic(bot, thread_id, path)

    att_block = ""
    if local_paths:
        att_block = (
            "\n\nAttachments on this reply (read them):\n"
            + "\n".join(f"- {p}" for p in local_paths)
        )

    reply_prompt = (
        f"The user replied on ticket {ticket_id}:\n\n"
        f"{content or '(no text content)'}"
        f"{att_block}\n\n"
        f"Incorporate this into your investigation. If you still need more information, draft another question, "
        f"show it to me, and WAIT for my approval before sending. Once approved, send as Homi via:\n"
        f"  `python3 {HOMI_REPLY_PATH} --ticket-id {ticket_id}` (body on stdin).\n"
        f"Exception: a pure thank-you / acknowledgment reply can be sent directly without approval."
    )

    state = get_state(SUPPORT_GROUP_ID, thread_id)
    try:
        chat_obj = await bot.get_chat(SUPPORT_GROUP_ID)
        async with state.processing_lock:
            await run_claude_streaming(
                reply_prompt,
                chat_obj,
                reply_to=None,
                state=state,
                thread_id=thread_id,
                cwd_override=SUPPORT_PROJECT_DIR,
            )
    except Exception as e:
        logger.error("Re-investigation after reply failed (ticket %s): %s", ticket_id, e, exc_info=True)


async def poll_ticket_replies(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: detect new user replies on dispatched tickets and forward them into their forum topic."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and SUPPORT_GROUP_ID):
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            tickets = await _fetch_active_dispatched_tickets(client)
            for ticket in tickets:
                ticket_id = ticket.get("id")
                thread_id = ticket.get("telegram_topic_id")
                if not ticket_id or not thread_id:
                    continue
                notes = ticket.get("notes") or []
                seen = _seen_ticket_notes.setdefault(ticket_id, set())

                # First time this ticket is observed by the reply poller: seed seen-set, don't replay history.
                if ticket_id not in _first_sight_tickets:
                    for n in notes:
                        if isinstance(n, dict) and n.get("id"):
                            seen.add(n["id"])
                    _first_sight_tickets.add(ticket_id)
                    continue

                for note in notes:
                    if not isinstance(note, dict):
                        continue
                    nid = note.get("id")
                    if not nid or nid in seen:
                        continue
                    seen.add(nid)
                    # Only fire for end-user replies — skip staff/homi notes we (or humans) wrote.
                    if note.get("author_role") != "user":
                        continue
                    try:
                        await _handle_new_user_note(context.bot, client, ticket, int(thread_id), note)
                    except Exception as e:
                        logger.error("Reply handling failed (ticket %s, note %s): %s", ticket_id, nid, e, exc_info=True)
    except Exception as e:
        logger.error("poll_ticket_replies error: %s", e, exc_info=True)


async def _fetch_pending_archive_tickets(client: httpx.AsyncClient) -> list[dict]:
    """Closed/resolved tickets with a Telegram topic that haven't been archived yet."""
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/support_tickets",
        params={
            "select": "id,telegram_topic_id",
            "telegram_topic_id": "not.is.null",
            "telegram_archived_at": "is.null",
            "status": "in.(closed,resolved)",
            "order": "resolved_at.asc.nullslast",
            "limit": "20",
        },
        headers=_supabase_headers(),
    )
    resp.raise_for_status()
    return resp.json()


async def _mark_ticket_archived(client: httpx.AsyncClient, ticket_id: str) -> None:
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/support_tickets",
        params={"id": f"eq.{ticket_id}"},
        json={"telegram_archived_at": datetime.now(timezone.utc).isoformat()},
        headers=_supabase_headers(),
    )
    resp.raise_for_status()


async def poll_ticket_archives(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: delete forum topics for tickets that have been resolved."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and SUPPORT_GROUP_ID):
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            tickets = await _fetch_pending_archive_tickets(client)
            if not tickets:
                return
            logger.info("Deleting %d closed ticket topic(s)", len(tickets))
            for ticket in tickets:
                ticket_id = ticket.get("id")
                topic_id = ticket.get("telegram_topic_id")
                if not ticket_id or not topic_id:
                    continue
                missing_topic = False
                try:
                    await context.bot.delete_forum_topic(
                        chat_id=SUPPORT_GROUP_ID,
                        message_thread_id=int(topic_id),
                    )
                except Exception as e:
                    msg = str(e).lower()
                    # If the topic was already deleted by a human, stop retrying.
                    if "not found" in msg or "topic_not_found" in msg or "thread not found" in msg:
                        logger.warning("Topic %s for ticket %s already gone — marking archived", topic_id, ticket_id)
                        missing_topic = True
                    else:
                        logger.error("Delete failed for ticket %s (topic %s): %s", ticket_id, topic_id, e)
                        continue

                try:
                    await _mark_ticket_archived(client, ticket_id)
                    _seen_ticket_notes.pop(ticket_id, None)
                    _first_sight_tickets.discard(ticket_id)
                    if not missing_topic:
                        logger.info("Deleted topic %s for ticket %s", topic_id, ticket_id)
                except Exception as e:
                    logger.error("Failed to mark ticket %s archived: %s", ticket_id, e)
    except Exception as e:
        logger.error("poll_ticket_archives error: %s", e, exc_info=True)


# ─── Support-ticket webhook listener ──────────────────────────────────────────
#
# The webhook is a self-contained, Supabase-free way to drive the support flow
# from any web app / codebase. Each ticket carries a `reply_url` that the bot
# POSTs back to when Homi sends a message — so one bot can serve many code-
# bases without any shared secret about them.

TICKET_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticket_threads.json")


def _load_ticket_store() -> dict:
    try:
        with open(TICKET_STORE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("Failed to load ticket store: %s", e)
        return {}


def _save_ticket_store(data: dict) -> None:
    try:
        tmp = TICKET_STORE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, TICKET_STORE_PATH)
    except Exception as e:
        logger.error("Failed to save ticket store: %s", e)


_ticket_threads: dict = _load_ticket_store()


def _record_ticket(ticket_id: str, thread_id: int, ticket: dict) -> None:
    _ticket_threads[ticket_id] = {
        "thread_id": thread_id,
        "reply_url": ticket.get("reply_url", ""),
        "reply_token": ticket.get("reply_token", ""),
        "project_dir": ticket.get("project_dir", "") or SUPPORT_PROJECT_DIR,
        "user_name": ticket.get("user_name", ""),
        "user_email": ticket.get("user_email", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_ticket_store(_ticket_threads)


def _resolve_ticket(payload: dict) -> tuple[str | None, int | None, dict]:
    """Resolve {ticket_id, thread_id} from a reply/resolved payload."""
    ticket_id = payload.get("ticket_id")
    thread_id = payload.get("thread_id")
    info: dict = {}
    if ticket_id and ticket_id in _ticket_threads:
        info = _ticket_threads[ticket_id]
        thread_id = thread_id or info.get("thread_id")
    elif thread_id:
        for tid, i in _ticket_threads.items():
            if i.get("thread_id") == thread_id:
                ticket_id, info = tid, i
                break
    if isinstance(thread_id, str) and thread_id.isdigit():
        thread_id = int(thread_id)
    return ticket_id, thread_id, info


async def _download_url(url: str, filename: str | None = None) -> str | None:
    """Download a URL to /tmp; return the local path or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.warning("Failed to fetch attachment %s: %s", url, e)
        return None
    safe_name = filename or url.rsplit("/", 1)[-1].split("?")[0] or "attachment"
    path = f"/tmp/tg_att_{uuid.uuid4().hex}_{safe_name}"
    try:
        with open(path, "wb") as f:
            f.write(resp.content)
        return path
    except Exception as e:
        logger.warning("Failed to write attachment %s: %s", path, e)
        return None


def _build_intro(ticket: dict) -> str:
    ticket_id = ticket.get("id", "?")
    user_name = ticket.get("user_name") or "Unknown"
    lines = ["*New support ticket*", ""]
    lines.append(f"*User:* {user_name}" + (f" (`{ticket['user_id']}`)" if ticket.get("user_id") else ""))
    if ticket.get("user_email"):    lines.append(f"*Email:* {ticket['user_email']}")
    if ticket.get("ticket_type"):   lines.append(f"*Type:* {ticket['ticket_type']}")
    if ticket.get("current_page"):  lines.append(f"*Page:* `{ticket['current_page']}`")
    if ticket.get("device_type"):   lines.append(f"*Device:* {ticket['device_type']}")
    lines.append(f"*Ticket ID:* `{ticket_id}`")
    meta = ticket.get("metadata") or {}
    if isinstance(meta, dict):
        for k, v in meta.items():
            lines.append(f"*{k}:* {v}")
    lines.append("")
    lines.append(f"*Message:*\n{ticket.get('message') or ''}")
    return "\n".join(lines)


async def _dispatch_webhook_ticket(bot, ticket: dict) -> int | None:
    """Create a forum topic for an incoming webhook ticket and return its thread_id.

    Webhook tickets are self-contained — no Supabase reads/writes happen here.
    Attachments are fetched from the URLs the payload provides.
    """
    ticket_id = ticket["id"]
    user_name = ticket.get("user_name") or "Unknown"
    first_line = (ticket.get("message") or "").strip().split("\n")[0]
    snippet = first_line[:40] + ("…" if len(first_line) > 40 else "")
    topic_name = f"{user_name[:20]} — {snippet}" if snippet else f"Ticket {ticket_id[:8]}"

    try:
        topic = await bot.create_forum_topic(chat_id=SUPPORT_GROUP_ID, name=topic_name)
    except Exception as e:
        logger.error("create_forum_topic failed for %s: %s", ticket_id, e)
        return None
    thread_id = topic.message_thread_id

    try:
        await bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=thread_id,
            text=markdownify(_build_intro(ticket)),
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error("Posting intro to topic %s failed: %s", thread_id, e)

    attachment_paths: list[str] = []
    for att in (ticket.get("attachments") or []):
        url = (att or {}).get("url")
        if not url:
            continue
        path = await _download_url(url, (att or {}).get("filename"))
        if path:
            attachment_paths.append(path)
            await _send_attachment_to_topic(bot, thread_id, path)

    _record_ticket(ticket_id, thread_id, ticket)
    # Keep reply-poller bookkeeping happy if the Supabase fallback is also active.
    _first_sight_tickets.add(ticket_id)
    _seen_ticket_notes.setdefault(ticket_id, set())

    asyncio.create_task(_investigate_webhook_ticket(bot, ticket, thread_id, attachment_paths))
    return thread_id


async def _investigate_webhook_ticket(bot, ticket: dict, thread_id: int, attachment_paths: list[str]) -> None:
    ticket_id = ticket["id"]
    project_dir = ticket.get("project_dir") or SUPPORT_PROJECT_DIR
    user_name = ticket.get("user_name") or "Unknown"

    att_block = ""
    if attachment_paths:
        att_block = "\n\nAttachments saved locally:\n" + "\n".join(f"- {p}" for p in attachment_paths)

    investigation_prompt = (
        f"You're investigating a support ticket from {user_name} ({ticket.get('user_email','?')}).\n\n"
        f"Ticket id: {ticket_id}\n"
        f"Page: {ticket.get('current_page','?')}\n"
        f"Device: {ticket.get('device_type','?')}\n"
        f"Type: {ticket.get('ticket_type','?')}\n\n"
        f"*Their message:*\n{ticket.get('message','')}\n"
        f"{att_block}\n\n"
        f"Investigation steps:\n"
        f"1. Investigate the codebase at {project_dir} for the root cause.\n"
        f"2. If you need more info from the user, DRAFT a reply, show me in this thread, "
        f"WAIT for my approval (e.g. \"yes\", \"send it\", \"go\"). Once approved, send by piping "
        f"the body into:\n"
        f"   `python3 {HOMI_REPLY_PATH} --ticket-id {ticket_id}`\n"
        f"   (the helper looks up the reply URL from this ticket's record and POSTs there).\n"
        f"   Exception: pure thank-you / acknowledgment replies can be sent directly.\n"
        f"3. Identify the likely root cause and propose a specific fix.\n\n"
        f"DO NOT modify any code yet — wait for my approval."
    )

    state = get_state(SUPPORT_GROUP_ID, thread_id)
    state.session_id = None  # fresh Claude session per ticket
    try:
        chat_obj = await bot.get_chat(SUPPORT_GROUP_ID)
        async with state.processing_lock:
            await run_claude_streaming(
                investigation_prompt, chat_obj, reply_to=None,
                state=state, thread_id=thread_id, cwd_override=project_dir,
            )
    except Exception as e:
        logger.error("Investigation failed for ticket %s: %s", ticket_id, e, exc_info=True)


async def _handle_webhook_reply(bot, ticket_id: str, thread_id: int, content: str, info: dict) -> None:
    project_dir = (info or {}).get("project_dir") or SUPPORT_PROJECT_DIR
    prompt = (
        f"The user replied on ticket {ticket_id}:\n\n{content or '(no text content)'}\n\n"
        f"Incorporate this into your investigation. If you still need more information, draft "
        f"another question, show it to me, and WAIT for my approval before sending. Once approved, "
        f"send as Homi via:\n"
        f"  `python3 {HOMI_REPLY_PATH} --ticket-id {ticket_id}` (body on stdin).\n"
        f"Exception: a pure thank-you / acknowledgment reply can be sent directly."
    )
    state = get_state(SUPPORT_GROUP_ID, thread_id)
    try:
        chat_obj = await bot.get_chat(SUPPORT_GROUP_ID)
        async with state.processing_lock:
            await run_claude_streaming(
                prompt, chat_obj, reply_to=None, state=state,
                thread_id=thread_id, cwd_override=project_dir,
            )
    except Exception as e:
        logger.error("Reply handler failed for ticket %s: %s", ticket_id, e, exc_info=True)


def _check_webhook_auth(request) -> "aioweb.Response | None":  # noqa: F821
    from aiohttp import web as aioweb
    if not SUPPORT_WEBHOOK_TOKEN:
        return aioweb.json_response({"error": "webhook not configured"}, status=503)
    if SUPPORT_GROUP_ID is None:
        return aioweb.json_response({"error": "SUPPORT_GROUP_ID not set on bot"}, status=503)
    if request.headers.get("Authorization", "") != f"Bearer {SUPPORT_WEBHOOK_TOKEN}":
        return aioweb.json_response({"error": "unauthorized"}, status=401)
    return None


async def _support_webhook_handler(request):
    """POST /support-ticket — open a forum topic for a new ticket.

    Body: {
      "id":           "<uuid>",       // required, unique per ticket
      "user_name":    "...",
      "user_email":   "...",
      "user_id":      "...",
      "ticket_type":  "...",
      "current_page": "/...",
      "device_type":  "...",
      "message":      "...",
      "metadata":     { ... },        // optional flat key/value, shown in intro
      "attachments":  [ {"url":"https://...", "filename":"..."} ],
      "project_dir":  "/path/to/repo",// optional override of SUPPORT_PROJECT_DIR
      "reply_url":    "https://...",  // bot POSTs Homi replies here
      "reply_token":  "..."           // sent as Bearer in the reply POST
    }
    """
    from aiohttp import web as aioweb
    err = _check_webhook_auth(request)
    if err is not None:
        return err
    try:
        ticket = await request.json()
    except Exception as e:
        return aioweb.json_response({"error": f"invalid json: {e}"}, status=400)
    if not isinstance(ticket, dict) or not ticket.get("id"):
        return aioweb.json_response({"error": "missing required field: id"}, status=400)
    if ticket["id"] in _ticket_threads:
        # Idempotent: return the existing thread instead of opening a duplicate.
        existing = _ticket_threads[ticket["id"]]
        return aioweb.json_response(
            {"ok": True, "ticket_id": ticket["id"], "thread_id": existing.get("thread_id"), "duplicate": True}
        )

    bot = request.app["bot"]
    logger.info("Webhook: dispatching ticket %s", ticket["id"])
    thread_id = await _dispatch_webhook_ticket(bot, ticket)
    if thread_id is None:
        return aioweb.json_response({"error": "dispatch failed"}, status=500)
    return aioweb.json_response({"ok": True, "ticket_id": ticket["id"], "thread_id": thread_id})


async def _support_reply_handler(request):
    """POST /support-ticket/reply — your web app calls this when the end user
    replies. Body: {ticket_id | thread_id, content, attachments, user_name}.
    """
    from aiohttp import web as aioweb
    err = _check_webhook_auth(request)
    if err is not None:
        return err
    try:
        payload = await request.json()
    except Exception as e:
        return aioweb.json_response({"error": f"invalid json: {e}"}, status=400)

    ticket_id, thread_id, info = _resolve_ticket(payload)
    if not thread_id:
        return aioweb.json_response({"error": "unknown ticket — provide ticket_id or thread_id"}, status=404)
    content = (payload.get("content") or "").strip()
    attachments = payload.get("attachments") or []
    if not content and not attachments:
        return aioweb.json_response({"error": "empty reply (need content or attachments)"}, status=400)

    bot = request.app["bot"]
    user_name = payload.get("user_name") or info.get("user_name") or "User"
    body = f"*{user_name} replied:*\n\n{content or '(no text)'}"
    try:
        await bot.send_message(
            chat_id=SUPPORT_GROUP_ID, message_thread_id=int(thread_id),
            text=markdownify(body), parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error("Posting reply to topic %s failed: %s", thread_id, e)
        return aioweb.json_response({"error": str(e)}, status=500)

    for att in attachments:
        url = (att or {}).get("url")
        if not url:
            continue
        path = await _download_url(url, (att or {}).get("filename"))
        if path:
            await _send_attachment_to_topic(bot, int(thread_id), path)

    if ticket_id:
        asyncio.create_task(_handle_webhook_reply(bot, ticket_id, int(thread_id), content, info))
    return aioweb.json_response({"ok": True})


async def _support_resolved_handler(request):
    """POST /support-ticket/resolved — close the topic for a resolved ticket.
    Body: {ticket_id | thread_id}.
    """
    from aiohttp import web as aioweb
    err = _check_webhook_auth(request)
    if err is not None:
        return err
    try:
        payload = await request.json()
    except Exception as e:
        return aioweb.json_response({"error": f"invalid json: {e}"}, status=400)

    ticket_id, thread_id, _info = _resolve_ticket(payload)
    if not thread_id:
        return aioweb.json_response({"error": "unknown ticket"}, status=404)

    bot = request.app["bot"]
    try:
        await bot.delete_forum_topic(chat_id=SUPPORT_GROUP_ID, message_thread_id=int(thread_id))
    except Exception as e:
        msg = str(e).lower()
        if not ("not found" in msg or "topic_not_found" in msg or "thread not found" in msg):
            logger.warning("delete_forum_topic for %s failed: %s", thread_id, e)
    if ticket_id and ticket_id in _ticket_threads:
        del _ticket_threads[ticket_id]
        _save_ticket_store(_ticket_threads)
        _seen_ticket_notes.pop(ticket_id, None)
        _first_sight_tickets.discard(ticket_id)
    return aioweb.json_response({"ok": True})


async def _start_support_webhook(application):
    from aiohttp import web as aioweb
    aio_app = aioweb.Application()
    aio_app["bot"] = application.bot
    aio_app.router.add_post("/support-ticket",          _support_webhook_handler)
    aio_app.router.add_post("/support-ticket/reply",    _support_reply_handler)
    aio_app.router.add_post("/support-ticket/resolved", _support_resolved_handler)
    aio_app.router.add_get("/health", lambda r: aioweb.json_response({"ok": True}))
    runner = aioweb.AppRunner(aio_app)
    await runner.setup()
    site = aioweb.TCPSite(runner, SUPPORT_WEBHOOK_BIND, SUPPORT_WEBHOOK_PORT)
    await site.start()
    logger.info(
        "Support webhook listening on http://%s:%d  (endpoints: /support-ticket, /support-ticket/reply, /support-ticket/resolved)",
        SUPPORT_WEBHOOK_BIND, SUPPORT_WEBHOOK_PORT,
    )
    return runner


async def _on_startup(application):
    if SUPPORT_WEBHOOK_PORT and SUPPORT_WEBHOOK_TOKEN and SUPPORT_GROUP_ID is not None:
        try:
            application._webhook_runner = await _start_support_webhook(application)
        except Exception as e:
            logger.error("Failed to start support webhook: %s", e, exc_info=True)


async def _on_shutdown(application):
    runner = getattr(application, "_webhook_runner", None)
    if runner is not None:
        try:
            await runner.cleanup()
        except Exception as e:
            logger.warning("webhook cleanup error: %s", e)


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("term", term_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("codex", codex_command))
    app.add_handler(CommandHandler("ollama", ollama_command))
    app.add_handler(CommandHandler("attach", attach_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and SUPPORT_GROUP_ID:
        app.job_queue.run_repeating(poll_tickets, interval=SUPPORT_POLL_INTERVAL, first=5)
        app.job_queue.run_repeating(poll_ticket_replies, interval=SUPPORT_REPLY_POLL_INTERVAL, first=15)
        app.job_queue.run_repeating(poll_ticket_archives, interval=SUPPORT_ARCHIVE_POLL_INTERVAL, first=30)
        logger.info(
            "Support pollers enabled (new=%ds, replies=%ds, archives=%ds, group=%s)",
            SUPPORT_POLL_INTERVAL, SUPPORT_REPLY_POLL_INTERVAL, SUPPORT_ARCHIVE_POLL_INTERVAL, SUPPORT_GROUP_ID,
        )
    else:
        logger.info("Support ticket poller disabled (missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / SUPPORT_GROUP_ID)")

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

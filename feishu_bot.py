#!/usr/bin/env python3
"""
Feishu (Lark) bot — mirrors telegram_bot.py / discord_bot.py.

Requirements:
    pip install lark-oapi

Config keys in config.json:
    feishu_app_id            – App ID from Feishu Open Platform
    feishu_app_secret        – App Secret
    feishu_encrypt_key       – Encrypt Key (leave "" if not set)
    feishu_verification_token – Verification Token (leave "" if not set)
    feishu_allowed_users     – list of open_id strings to whitelist ([] = allow all)

The bot uses the WebSocket long-connection mode so no public endpoint is needed.
Enable "Use Bot" and subscribe to the "im.message.receive_v1" event in the
Feishu Open Platform developer console. Also grant these permissions:
    im:message          – read messages
    im:message:send_as_bot – send messages
    im:resource         – download message resources (files/images)
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from agent_core import Agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("feishu_bot")

# Directory where attachments are saved so the agent can read them via tools.
ATTACHMENTS_DIR = project_root / "attachments"
ATTACHMENTS_DIR.mkdir(exist_ok=True)


# ── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path) as f:
        return json.load(f)


cfg = load_config()
APP_ID = cfg.get("feishu_app_id", "").strip()
APP_SECRET = cfg.get("feishu_app_secret", "").strip()
ENCRYPT_KEY = cfg.get("feishu_encrypt_key", "").strip()
VERIFICATION_TOKEN = cfg.get("feishu_verification_token", "").strip()

ALLOWED_USERS: list[str] = [
    str(u) for u in cfg.get("feishu_allowed_users", []) if u
]

if not APP_ID or not APP_SECRET:
    logger.error("feishu_app_id / feishu_app_secret not set in config.json. Exiting.")
    sys.exit(1)

# ── Lark client (used for sending messages & downloading files) ───────────────

lark_client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .log_level(lark.LogLevel.WARNING) \
    .build()

# ── Agent ─────────────────────────────────────────────────────────────────────

agent: Agent | None = None


def init_agent() -> Agent:
    global agent
    if agent is None:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        agent = Agent(config_path=config_path)
    return agent


# ── Progress formatting ───────────────────────────────────────────────────────

def _format_progress(ev: dict) -> str:
    t = ev.get("type")
    if t == "thinking":
        return f"🤔 Thinking… (iteration {ev.get('iteration', '?')})"
    if t == "tool_call":
        tool = ev.get("tool", "?")
        try:
            args = json.loads(ev.get("args", "{}"))
            preview = ", ".join(f"{k}={repr(v)}" for k, v in list(args.items())[:2])
        except Exception:
            preview = ev.get("args", "")
        return f"⚡ {tool}({preview[:177]}...)"
    if t == "tool_result":
        result = ev.get("result", "")
        return f"💾 {result[:177]}…"
    return "⏳ Working…"


# ── Feishu messaging helpers ──────────────────────────────────────────────────

def _send_text(chat_id: str, text: str) -> str | None:
    """Send a new text message to chat_id; returns message_id or None."""
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}))
            .build()
        )
        .build()
    )
    resp = lark_client.im.v1.message.create(req)
    if not resp.success():
        logger.error(f"send_text failed: {resp.code} {resp.msg}")
        return None
    return resp.data.message_id


def _reply_text(message_id: str, text: str) -> str | None:
    """Reply to a specific message; returns new message_id or None."""
    # Feishu limits message content to 30 000 chars; we chunk if needed.
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    last_id = None
    for chunk in chunks:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": chunk}))
                .build()
            )
            .build()
        )
        resp = lark_client.im.v1.message.reply(req)
        if not resp.success():
            logger.error(f"reply_text failed: {resp.code} {resp.msg}")
        else:
            last_id = resp.data.message_id
    return last_id


def _update_message(message_id: str, text: str):
    """
    Feishu does not support editing arbitrary messages; we track the last
    status message_id and delete+resend when needed. Because the Feishu API
    also does not expose a simple "edit text message" endpoint for bots in all
    plans, we use reply-chaining: the first status reply is sent once and we
    track it. For streaming updates we just send a new reply for the final
    answer and note that intermediate progress is fire-and-forget (not edited
    in-place) to stay within rate limits.
    This function is intentionally a no-op placeholder so the event loop below
    can call it without branching.
    """
    pass  # See drain_queue() for the actual update strategy.


# ── File download ─────────────────────────────────────────────────────────────

def _download_resource(message_id: str, file_key: str, msg_type: str) -> str | None:
    """
    Download a file or image attached to a Feishu message.

    msg_type should be the Feishu message type string, e.g. "image", "file",
    "audio", "media", "sticker".  The API resource type is either "image" or
    "file" depending on the category.

    Returns the local path where the file was saved, or None on failure.
    """
    # Feishu resource type is "image" for images/stickers, "file" for everything else.
    resource_type = "image" if msg_type in ("image", "sticker") else "file"

    # Use file_key as filename base to avoid collisions.
    ext_map = {"image": ".jpg", "sticker": ".png", "audio": ".opus",
               "media": ".mp4", "file": ""}
    ext = ext_map.get(msg_type, "")
    local_path = ATTACHMENTS_DIR / f"{file_key}{ext}"

    if local_path.exists():
        logger.info(f"Resource already cached: {local_path}")
        return str(local_path)

    req = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(file_key)
        .type(resource_type)
        .build()
    )
    resp = lark_client.im.v1.message_resource.get(req)
    if not resp.success():
        logger.error(f"download_resource failed: {resp.code} {resp.msg}")
        return None

    # resp.data.file_content is a bytes-like object (lark_oapi wraps the stream).
    with open(local_path, "wb") as fh:
        fh.write(resp.data.file_content.read())

    logger.info(f"Resource saved: {local_path}")
    return str(local_path)


# ── Message content extraction ────────────────────────────────────────────────

def _extract_text_and_files(event: P2ImMessageReceiveV1) -> tuple[str, list[str]]:
    """
    Parse a Feishu message event and return (plain_text, [saved_file_paths]).

    Feishu sends message content as a JSON string whose schema varies by
    msg_type.  We handle: text, post (rich text), image, file, audio, media,
    sticker.  Unknown types fall back to an empty string.
    """
    msg = event.event.message
    msg_type = msg.message_type  # e.g. "text", "image", "file", …
    message_id = msg.message_id
    saved_paths: list[str] = []
    plain_text = ""

    try:
        content = json.loads(msg.content or "{}")
    except json.JSONDecodeError:
        content = {}

    if msg_type == "text":
        # {"text": "@_user_1 hello"}  — strip @-mentions
        raw = content.get("text", "")
        # Remove Feishu mention tokens like @_user_xxx
        import re
        plain_text = re.sub(r"@_\S+", "", raw).strip()

    elif msg_type == "post":
        # Rich text: {"zh_cn": {"title": "…", "content": [[{"tag":"text","text":"…"}]]}}
        # Flatten all text nodes across all languages (prefer zh_cn or first available).
        post = content.get("zh_cn") or next(iter(content.values()), {})
        texts = []
        for line in post.get("content", []):
            for node in line:
                if node.get("tag") == "text":
                    texts.append(node.get("text", ""))
        plain_text = " ".join(texts).strip()

    elif msg_type in ("image", "sticker"):
        file_key = content.get("image_key", "")
        if file_key:
            path = _download_resource(message_id, file_key, msg_type)
            if path:
                saved_paths.append(path)

    elif msg_type in ("file", "audio", "media"):
        file_key = content.get("file_key", "")
        if file_key:
            path = _download_resource(message_id, file_key, msg_type)
            if path:
                saved_paths.append(path)

    return plain_text, saved_paths


# ── Core message handler ──────────────────────────────────────────────────────

def handle_message(event: P2ImMessageReceiveV1):
    """
    Called by the SDK on every im.message.receive_v1 event.
    Runs synchronously in the SDK's event thread; we spawn our own thread
    for the agent and drain events in a fresh asyncio loop — mirroring the
    Discord bot's pattern.
    """
    msg = event.event.message
    sender = event.event.sender
    chat_id = msg.chat_id
    message_id = msg.message_id
    sender_id = sender.sender_id.open_id if sender.sender_id else ""

    # Authorization check
    if ALLOWED_USERS and sender_id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized Feishu user {sender_id} in chat {chat_id}.")
        _reply_text(message_id, "Sorry, you are not authorized to use this bot.")
        return

    # /new command
    msg_type = msg.message_type
    try:
        raw_content = json.loads(msg.content or "{}")
    except json.JSONDecodeError:
        raw_content = {}
    raw_text = raw_content.get("text", "").strip() if msg_type == "text" else ""

    if raw_text in ("/new", "new/"):
        try:
            init_agent().clear_history(chat_id)
            _reply_text(message_id, "✅ New conversation started. Previous messages cleared.")
        except Exception as e:
            logger.exception(f"Error clearing history for chat {chat_id}: {e}")
            _reply_text(message_id, f"Error: {e}")
        return

    # Extract text + download any attached files
    plain_text, saved_paths = _extract_text_and_files(event)

    # Build the final text the agent sees
    if saved_paths:
        paths_note = "\n".join(f"[Attached file saved to: {p}]" for p in saved_paths)
        text = f"{plain_text}\n\n{paths_note}".strip()
    else:
        text = plain_text

    if not text:
        _reply_text(message_id, "Please send a message or attach a file.")
        return

    logger.info(f"Message from {sender_id} in {chat_id}: '{text[:80]}'")

    # Send initial status reply
    status_message_id = _reply_text(message_id, "⏳ Working…")

    step_queue: queue.Queue = queue.Queue()

    def step_callback(ev: dict):
        step_queue.put(ev)

    def run_agent():
        try:
            init_agent().chat(text, chat_id, step_callback=step_callback)
        except Exception as e:
            step_queue.put({"type": "error", "content": str(e)})
        finally:
            step_queue.put(None)  # sentinel

    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    # ── Drain the step queue in a synchronous polling loop ───────────────────
    # (We don't use asyncio here because handle_message is called from the SDK's
    # synchronous callback thread.  Feishu's "edit message" API is not available
    # for all bot types, so we use a simple strategy:
    #   • Progress updates (thinking / tool_call / tool_result) are sent as new
    #     replies to keep the user informed without requiring edit support.
    #   • We rate-limit these to at most one every 2 s to avoid flooding.
    #   • The final answer is sent as a new reply, and the initial "Working…"
    #     placeholder is left as-is (editing is unsupported in most configs).)

    import time

    last_progress_time = time.time()
    PROGRESS_MIN_INTERVAL = 2.0   # seconds between progress updates
    streamed_chunks: list[str] = []

    while True:
        try:
            ev = step_queue.get(timeout=0.2)
        except queue.Empty:
            continue

        if ev is None:
            break

        ev_type = ev.get("type")

        if ev_type == "text_delta":
            streamed_chunks.append(ev.get("content", ""))

        elif ev_type == "thinking":
            now = time.time()
            if now - last_progress_time >= PROGRESS_MIN_INTERVAL:
                _reply_text(
                    message_id,
                    f"🧠 Thinking… (step {ev.get('iteration', '?')})"
                )
                last_progress_time = now
            streamed_chunks = []  # reset accumulated text for this new LLM turn

        elif ev_type in ("tool_call", "tool_result"):
            now = time.time()
            if now - last_progress_time >= PROGRESS_MIN_INTERVAL:
                _reply_text(message_id, _format_progress(ev))
                last_progress_time = now

        elif ev_type == "final":
            final_text = ev.get("content", "")
            tokens = ev.get("tokens")
            token_note = f"\n\n💰 Tokens 🔥: {tokens:,}" if tokens else ""
            full_final = final_text + token_note
            _reply_text(message_id, full_final)
            break

        elif ev_type == "error":
            err = ev.get("content", "Unknown error")
            _reply_text(message_id, f"❌ Error: {err}")
            break

    thread.join(timeout=0)  # let the daemon thread clean up on its own


# ── Bot entry point ───────────────────────────────────────────────────────────

def main():
    event_handler = (
        lark.EventDispatcherHandler.builder(ENCRYPT_KEY, VERIFICATION_TOKEN)
        .register_p2_im_message_receive_v1(handle_message)
        .build()
    )

    ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    logger.info("Feishu bot starting (WebSocket mode)…")
    ws_client.start()


if __name__ == "__main__":
    main()

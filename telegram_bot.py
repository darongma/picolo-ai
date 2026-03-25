#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import queue
import sys
import threading
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from agent_core import Agent

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("telegram_bot")

agent = None

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, 'r') as f:
        return json.load(f)

_initial_cfg = load_config()
ALLOWED_USERS = []
for x in _initial_cfg.get("telegram_allowed_users", []):
    try:
        ALLOWED_USERS.append(int(x))
    except (ValueError, TypeError):
        pass

def init_agent():
    global agent
    if agent is None:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        agent = Agent(config_path=config_path)
    return agent


def _format_progress(ev) -> str:
    lines = []
    t = ev.get("type")
    if t == "thinking":
        lines.append(f"🤔 Thinking… (iteration {ev.get('iteration', '?')})")
    elif t == "tool_call":
        tool = ev.get("tool", "?")
        try:
            args = json.loads(ev.get("args", "{}"))
            # Show at most one key=value pair to keep it short
            preview = ", ".join(f"{k}={repr(v)}" for k, v in list(args.items())[:2])
        except Exception:
            preview = ev.get("args", "")
        preview=preview[:177]+"..."
        lines.append(f"⚡ `{tool}({preview})`")
    elif t == "tool_result":
        tool = ev.get("tool", "?")
        result = ev.get("result", "")
        # Truncate long results
        result = result[:177] + "…"
        lines.append(f"💾 {result}")
    return "\n".join(lines) if lines else "⏳ Working…"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    text = update.message.text or ""

    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized user {user.id} (chat {chat_id}) attempted to use bot.")
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    # Send an initial status message that we will edit in-place as the agent works.
    status_msg = await update.message.reply_text("⏳ Working…")

    step_queue: queue.Queue = queue.Queue()

    def step_callback(event: dict):
        step_queue.put(event)

    def run_agent():
        try:
            init_agent().chat(text, chat_id, step_callback=step_callback)
        except Exception as e:
            step_queue.put({"type": "error", "content": str(e)})
        finally:
            step_queue.put(None)  # sentinel

    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    # Accumulated streamed text from text_delta events.
    streamed_text = ""
    # Throttle Telegram edits: only push an update when enough new chars arrived
    # or enough time has passed (avoids hitting Telegram's rate limits).
    last_edited_text = ""
    EDIT_MIN_CHARS = 20   # minimum new characters before triggering an edit
    EDIT_MAX_WAIT = 1.5   # seconds: force an edit even if EDIT_MIN_CHARS not met

    last_edit_time = asyncio.get_event_loop().time()

    async def _edit_status(new_text: str):
        """Edit status_msg, silently ignoring 'message not modified' errors."""
        nonlocal last_edited_text, last_edit_time
        trimmed = new_text[:4096]
        if trimmed == last_edited_text:
            return
        try:
            await status_msg.edit_text(trimmed)
            last_edited_text = trimmed
            last_edit_time = asyncio.get_event_loop().time()
        except Exception:
            pass  # e.g. MessageNotModified — safe to ignore

    async def drain_queue():
        nonlocal streamed_text, last_edit_time

        while True:
            try:
                event = step_queue.get_nowait()
            except queue.Empty:
                # Flush any pending streamed text that hasn't been pushed yet
                if streamed_text and streamed_text != last_edited_text:
                    elapsed = asyncio.get_event_loop().time() - last_edit_time
                    if elapsed >= EDIT_MAX_WAIT:
                        await _edit_status(streamed_text)
                await asyncio.sleep(0.15)
                continue

            if event is None:
                break

            ev_type = event.get("type")

            if ev_type == "text_delta":
                # Append the new token and conditionally push an edit.
                streamed_text += event.get("content", "")
                new_chars = len(streamed_text) - len(last_edited_text)
                elapsed = asyncio.get_event_loop().time() - last_edit_time
                if new_chars >= EDIT_MIN_CHARS or elapsed >= EDIT_MAX_WAIT:
                    await _edit_status(streamed_text)

            elif ev_type == "thinking":
                iteration = event.get("iteration", "?")
                status = f"🧠 Thinking… (step {iteration})"
                streamed_text = ""          # reset for this new LLM turn
                last_edited_text = ""
                await _edit_status(status)

            elif ev_type == "tool_call":
                progress = _format_progress(event)
                await _edit_status(progress)

            elif ev_type == "tool_result":
                progress = _format_progress(event)
                await _edit_status(progress)

            elif ev_type == "final":
                final_text = event.get("content", "")
                tokens = event.get("tokens")
                token_note = f"\n\n💰 Tokens 🔥: {tokens:,}" if tokens else ""
                full_final = final_text + token_note

                # Replace the status message with the final answer.
                # If the answer fits in one message, edit in-place; otherwise
                # delete the status bubble and send fresh chunks.
                if len(full_final) <= 4096:
                    await _edit_status(full_final)
                else:
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
                    for i in range(0, len(full_final), 4096):
                        await update.message.reply_text(full_final[i:i + 4096])
                break

            elif ev_type == "error":
                err = event.get("content", "Unknown error")
                await _edit_status(f"❌ Error: {err}")
                break

    await drain_queue()


async def handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    try:
        init_agent().clear_history(chat_id)
        await update.message.reply_text("✅ New conversation started. Previous messages cleared.")
    except Exception as e:
        logger.exception(f"Error clearing history for chat {chat_id}: {e}")
        await update.message.reply_text(f"Error: {e}")


def main():
    cfg = load_config()
    token = cfg.get("telegram_token", "").strip()
    if not token:
        logger.error("No telegram_token set in config.json. Exiting.")
        sys.exit(1)

    application = Application.builder().token(token).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CommandHandler("new", handle_new))

    logger.info("Telegram bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
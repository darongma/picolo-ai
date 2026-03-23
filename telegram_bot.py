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


def _format_progress(events: list[dict]) -> str:
    """
    Build a human-readable progress string from accumulated step events.
    Each tool call/result pair is collapsed into a single line so the
    message stays compact as it grows.
    """
    lines = []
    for ev in events:
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
            lines.append(f"🔧 `{tool}({preview})`")
        elif t == "tool_result":
            tool = ev.get("tool", "?")
            result = ev.get("result", "")
            # Truncate long results
            if len(result) > 177:
                result = result[:177] + "…"
            lines.append(f"   ↳ {result}")
    return "\n".join(lines) if lines else "⏳ _Working…_"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    text = update.message.text or ""

    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized user {user.id} (chat {chat_id}) attempted to use bot.")
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    await update.message.reply_text("⏳ Working…")

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

    accumulated: list[dict] = []
    last_progress_text = ""

    async def drain_queue():
        nonlocal last_progress_text
        while True:
            try:
                event = step_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.3)
                continue

            if event is None:
                break

            ev_type = event.get("type")

            if ev_type == "final":
                final_text = event.get("content", "")
                tokens = event.get("tokens")
                token_note = f"\n\n💰 Tokens 🔥: {tokens:,}" if tokens else ""
                full_text = final_text + token_note
                for i in range(0, len(full_text), 4096):
                    await update.message.reply_text(full_text[i:i + 4096])
                break

            elif ev_type == "error":
                err = event.get("content", "Unknown error")
                await update.message.reply_text(f"❌ Error: {err}")
                break

            else:
                # thinking / tool_call / tool_result — send plain-text snippet (no parse_mode,
                # so a mid-entity slice never causes a Bad Request)
                accumulated.append(event)
                new_text = _format_progress(accumulated)
                if new_text != last_progress_text:
                    await update.message.reply_text(new_text)
                    last_progress_text = new_text

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
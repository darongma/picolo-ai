#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from agent_core import Agent

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("telegram_bot")

# Global agent instance (thread-safe)
agent = None

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, 'r') as f:
        return json.load(f)

# Load allowed users once at startup for efficiency
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    text = update.message.text or ""

    # Security: check allowed users if configured
    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized user {user.id} (chat {chat_id}) attempted to use bot.")
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return

    # Indicate typing
    await update.message.chat.send_action(action="typing")

    # Run agent in a thread to avoid blocking the async loop
    loop = asyncio.get_running_loop()
    try:
        response, total, history = await loop.run_in_executor(None, init_agent().chat, text, chat_id)
    except Exception as e:
        logger.exception(f"Agent error for chat {chat_id}: {e}")
        response = f"Error: {e}"

    # Telegram message limit is 4096 chars; split if necessary
    for i in range(0, len(response), 4096):
        chunk = response[i:i+4096]
        await update.message.reply_text(chunk)

async def handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    # Security check
    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return
    try:
        agent = init_agent()
        agent.clear_history(chat_id)
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

    # Build application
    application = Application.builder().token(token).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CommandHandler("new", handle_new))

    logger.info("Telegram bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

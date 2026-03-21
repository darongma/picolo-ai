#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from agent_core import Agent

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("discord_bot")

# Load config
def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, 'r') as f:
        return json.load(f)

cfg = load_config()
TOKEN = cfg.get("discord_token", "").strip()
# Convert allowed users to integers (IDs may be stored as strings)
ALLOWED_USERS = []
for x in cfg.get("discord_allowed_users", []):
    try:
        ALLOWED_USERS.append(int(x))
    except (ValueError, TypeError):
        pass

if not TOKEN:
    logger.error("No discord_token set in config.json. Exiting.")
    sys.exit(1)

# Set up intents
intents = discord.Intents.default()
intents.message_content = True  # required to read message text

bot = commands.Bot(command_prefix="!", intents=intents)

# Global agent instance (shared across events)
agent = None

async def get_agent():
    global agent
    if agent is None:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        agent = Agent(config_path=config_path)
    return agent

@bot.event
async def on_ready():
    logger.info(f'Discord bot logged in as {bot.user} (ID: {bot.user.id})')
    logger.info('------')

@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from bots (including ourselves)
    if message.author.bot:
        return

    # Allowed users check
    if ALLOWED_USERS and message.author.id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized Discord user {message.author.id} tried to use bot.")
        try:
            await message.channel.send("Sorry, you are not authorized to use this bot.")
        except Exception:
            pass
        return

    # Command: /new – start a new conversation (clear history)
    if message.content.strip() == 'new/':
        session_id = str(message.channel.id)
        try:
            ag = await get_agent()
            ag.clear_history(session_id)
            await message.reply("✅ New conversation started. Previous messages cleared.")
        except Exception as e:
            logger.exception(f"Error clearing history for Discord channel {session_id}: {e}")
            await message.reply(f"Error: {e}")
        return

    # Only respond to DMs or when mentioned in a guild channel
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions
    if not is_dm and not is_mentioned:
        return

    async with message.channel.typing():
        try:
            ag = await get_agent()
            # Use DM channel's ID or guild+channel ID as session_id
            session_id = str(message.channel.id)
            response, total, history = await asyncio.to_thread(ag.chat, message.content, session_id)
            # Discord limit is 2000 chars; split if necessary
            for i in range(0, len(response), 2000):
                chunk = response[i:i+2000]
                await message.reply(chunk)
        except Exception as e:
            logger.exception(f"Error handling Discord message: {e}")
            try:
                await message.reply(f"Error: {e}")
            except Exception:
                pass

def main():
    bot.run(TOKEN, log_handler=None)

if __name__ == "__main__":
    main()

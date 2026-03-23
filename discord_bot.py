#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import queue
import sys
import threading
from pathlib import Path

import discord
from discord.ext import commands

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from agent_core import Agent

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("discord_bot")


def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(config_path, 'r') as f:
        return json.load(f)


cfg = load_config()
TOKEN = cfg.get("discord_token", "").strip()
ALLOWED_USERS = []
for x in cfg.get("discord_allowed_users", []):
    try:
        ALLOWED_USERS.append(int(x))
    except (ValueError, TypeError):
        pass

if not TOKEN:
    logger.error("No discord_token set in config.json. Exiting.")
    sys.exit(1)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

agent = None


async def get_agent():
    global agent
    if agent is None:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        agent = Agent(config_path=config_path)
    return agent


def _format_progress(ev) -> str:
    lines = []
    t = ev.get("type")
    if t == "thinking":
        lines.append(f"🤔 *Thinking… (iteration {ev.get('iteration', '?')})*")
    elif t == "tool_call":
        tool = ev.get("tool", "?")
        try:
            args = json.loads(ev.get("args", "{}"))
            preview = ", ".join(f"{k}={repr(v)}" for k, v in list(args.items())[:2])
        except Exception:
            preview = ev.get("args", "")
        preview=preview[:177]+"..."
        lines.append(f"⚡ `{tool}({preview})`")
    elif t == "tool_result":
        result = ev.get("result", "")
        result = result[:177] + "…"
        lines.append(f"💾 {result}")
    return "\n".join(lines) if lines else "⏳ *Working…*"


@bot.event
async def on_ready():
    logger.info(f'Discord bot logged in as {bot.user} (ID: {bot.user.id})')
    logger.info('------')


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if ALLOWED_USERS and message.author.id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized Discord user {message.author.id} tried to use bot.")
        try:
            await message.channel.send("Sorry, you are not authorized to use this bot.")
        except Exception:
            pass
        return

    # Command: new/ — start a new conversation
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

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions
    if not is_dm and not is_mentioned:
        return

    session_id = str(message.channel.id)

    status_msg = await message.reply("⏳ Working…")

    step_queue: queue.Queue = queue.Queue()

    def step_callback(event: dict):
        step_queue.put(event)

    async def run_agent_async():
        ag = await get_agent()

        def _blocking():
            try:
                ag.chat(message.content, session_id, step_callback=step_callback)
            except Exception as e:
                step_queue.put({"type": "error", "content": str(e)})
            finally:
                step_queue.put(None)  # sentinel

        thread = threading.Thread(target=_blocking, daemon=True)
        thread.start()

    await run_agent_async()

    # Accumulated streamed text from text_delta events.
    streamed_text = ""
    last_edited_text = ""
    EDIT_MIN_CHARS = 20    # minimum new chars before pushing an edit
    EDIT_MAX_WAIT = 1.5    # seconds: force an edit even if threshold not met

    last_edit_time = asyncio.get_event_loop().time()

    async def _edit_status(new_text: str):
        """Edit status_msg in-place, silently ignoring Discord errors."""
        nonlocal last_edited_text, last_edit_time, status_msg
        trimmed = new_text[:2000]
        if trimmed == last_edited_text or status_msg is None:
            return
        try:
            await status_msg.edit(content=trimmed)
            last_edited_text = trimmed
            last_edit_time = asyncio.get_event_loop().time()
        except Exception:
            pass  # e.g. rate-limited or message deleted — safe to ignore

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
            streamed_text += event.get("content", "")
            new_chars = len(streamed_text) - len(last_edited_text)
            elapsed = asyncio.get_event_loop().time() - last_edit_time
            if new_chars >= EDIT_MIN_CHARS or elapsed >= EDIT_MAX_WAIT:
                await _edit_status(streamed_text)

        elif ev_type == "thinking":
            iteration = event.get("iteration", "?")
            streamed_text = ""          # reset for new LLM turn
            last_edited_text = ""
            await _edit_status(f"🧠 Thinking… (step {iteration})")

        elif ev_type in ("tool_call", "tool_result"):
            await _edit_status(_format_progress(event))

        elif ev_type == "final":
            final_text = event.get("content", "")
            tokens = event.get("tokens")
            token_note = f"\n\n💰 Tokens 🔥: {tokens:,}" if tokens else ""
            full_final = final_text + token_note

            if len(full_final) <= 2000:
                await _edit_status(full_final)
            else:
                # Too long to fit in one edit — delete status bubble and send chunks
                try:
                    if status_msg:
                        await status_msg.delete()
                        status_msg = None
                except Exception:
                    pass
                for i in range(0, len(full_final), 2000):
                    await message.reply(full_final[i:i + 2000])
            break

        elif ev_type == "error":
            err = event.get("content", "Unknown error")
            await _edit_status(f"❌ Error: {err}")
            break


def main():
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
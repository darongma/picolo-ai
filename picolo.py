#!/usr/bin/env python3
"""
Picolo – Lightweight AI agent with auto-starting web UI.

Usage:
  picolo              # start web UI (default)
  picolo --web        # same as above
  picolo --cli        # start CLI chat instead
  picolo --port 8000  # specify web server port
  picolo --no-browser # don't auto-open browser
"""
import sys
import os
import argparse
import webbrowser
import threading
import multiprocessing
from threading import Timer

def check_web_deps():
    try:
        import fastapi
        import uvicorn
        return True
    except ImportError:
        return False

def launch_web(port=8000, open_browser=True):
    web_dir = os.path.join(os.path.dirname(__file__), "web")
    if not os.path.exists(web_dir):
        print(f"Error: web directory not found at {web_dir}")
        return 1

    # Add web directory to path so we can import main.py
    sys.path.insert(0, web_dir)
    try:
        import main as web_main  # imports web/main.py
        import uvicorn
        import json

        # Check for bot tokens in config.json
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            # Telegram
            telegram_token = config.get("telegram_token", "").strip()
            if telegram_token:
                def run_telegram_bot():
                    try:
                        import telegram_bot
                        telegram_bot.main()
                    except Exception as e:
                        print(f"[Telegram bot error] {e}", file=sys.stderr)
                t = multiprocessing.Process(target=run_telegram_bot, daemon=True)
                t.start()
                print("Telegram bot starting in background…")
            else:
                print("No telegram_token set; Telegram bot disabled.")
            # Discord
            discord_token = config.get("discord_token", "").strip()
            if discord_token:
                def run_discord_bot():
                    try:
                        import discord_bot
                        discord_bot.main()
                    except Exception as e:
                        print(f"[Discord bot error] {e}", file=sys.stderr)
                t = multiprocessing.Process(target=run_discord_bot, daemon=True)
                t.start()
                print("Discord bot starting in background…")
            else:
                print("No discord_token set; Discord bot disabled.")
        except Exception as e:
            print(f"[Warning] Could not load config.json for bots: {e}", file=sys.stderr)

        if open_browser:
            def open_browser_tab():
                webbrowser.open(f"http://localhost:{port}")
            Timer(1.0, open_browser_tab).start()
            print(f"Opening browser to http://localhost:{port} …")
        else:
            print(f"Server starting at http://localhost:{port}")

        uvicorn.run(web_main.app, host="0.0.0.0", port=port)
    except ImportError as e:
        print("Missing dependencies for the web UI.")
        print("Please install them:")
        print("  pip install -r picolo/requirements.txt")
        print("  pip install -r picolo/web/requirements.txt")
        return 1
    except Exception as e:
        print(f"Error starting web server: {e}")
        return 1
    finally:
        sys.path.remove(web_dir)
    return 0

def launch_cli():
    from agent_core import Agent
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    agent = Agent(config_path)
    session_id = agent.config.get("session_id", "default")

    print("Picolo CLI. Type 'quit' or Ctrl+C to exit.\n")
    if agent.tools_dict:
        print(f"Loaded tools: {', '.join(agent.tools_dict.keys())}\n")

    try:
        while True:
            try:
                user_input = input("> ").strip()
                if user_input.lower() in ("quit", "exit"):
                    break
                if not user_input:
                    continue
                response = agent.chat(user_input, session_id)
                print(f"\n{response}\n")
            except KeyboardInterrupt:
                print("\nInterrupted. Type 'quit' to exit.\n")
            except Exception as e:
                print(f"[Error] {e}\n")
    finally:
        agent.close()
    return 0

def main():
    parser = argparse.ArgumentParser(description="Picolo – Lightweight AI agent")
    parser.add_argument("--cli", action="store_true", help="Start CLI chat mode")
    parser.add_argument("--web", action="store_true", help="Start web UI (default)")
    parser.add_argument("--port", type=int, default=8000, help="Port for web server (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    args = parser.parse_args()

    # Default to web if no mode specified
    if args.cli:
        sys.exit(launch_cli())
    else:
        # Web mode
        if not check_web_deps():
            print("Web UI dependencies missing. Install them first:")
            print("  pip install -r picolo/requirements.txt")
            print("  pip install -r picolo/web/requirements.txt")
            sys.exit(1)
        sys.exit(launch_web(port=args.port, open_browser=not args.no_browser))

if __name__ == "__main__":
    main()

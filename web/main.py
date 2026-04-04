"""
Picolo Web UI - FastAPI backend

Changes from original:
  - /api/chat/stream  : new SSE endpoint — streams tool call steps as they happen
  - /api/chat         : kept for backwards compatibility (non-streaming)
  - agent_lock        : now only guards config mutations (save_config, reload).
                        chat() runs concurrently — sessions are isolated by
                        session_id and the agent's internal per-operation locks.
  - Thread pool       : chat() is CPU/IO-bound and blocking; we run it in
                        FastAPI's default thread-pool via asyncio.to_thread()
                        so the async event loop is never blocked.
"""
import asyncio
import datetime
import json
import queue
import sys
import os
import threading
import uuid

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import uvicorn

# Add project root to Python path
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJECT_ROOT)

from agent_core import Agent

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

# Global references (set in lifespan)
agent = None

# This lock is ONLY for config mutations (save_config / reload).
# It must NOT be held during chat() — that would re-introduce serialisation.
config_lock = threading.RLock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent
    agent = Agent(CONFIG_PATH)
    yield
    if agent:
        agent.close()
    agent = None


app = FastAPI(title="Picolo Web UI", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sse_line(event_type: str, data: dict) -> str:
    """Format a single SSE frame."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok"}


# ── Gemini Live Token ─────────────────────────────────────────────────────────

@app.post("/api/gemini-token")
async def get_gemini_token():
    """
    Generates a short-lived ephemeral token for the Gemini Live WebSocket API.
    The browser uses this token directly — your GEMINI_API_KEY never leaves the server.
    Requires: pip install google-genai
    """
    try:
        import os as _os
        api_key = _os.environ.get("GEMINI_API_KEY") or agent.config.get("gemini_api_key", "").strip()
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail="GEMINI_API_KEY not set. Add it to config.json as 'gemini_api_key' or set the env var."
            )
        from google import genai
        client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        expire_time = now + datetime.timedelta(minutes=30)
        token = client.auth_tokens.create(
            config={
                "uses": 1,
                "expire_time": expire_time.isoformat(),
                "new_session_expire_time": (now + datetime.timedelta(minutes=1)).isoformat(),
                "http_options": {"api_version": "v1alpha"},
            }
        )
        return {"token": token.name, "expires_at": expire_time.isoformat()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create Gemini token: {e}")


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get("/api/logs")
def get_logs(limit: int = 200):
    """Return the last N log lines from in-memory buffer, falling back to file."""
    try:
        logs = agent.get_recent_logs(limit)
        return {"logs": logs}
    except Exception:
        log_path = agent.config.get("log_file", os.path.join(PROJECT_ROOT, "picolo.log"))
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            recent = lines[-limit:] if lines else []
            return {"logs": recent}
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Log file not found")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


# ── Config ────────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    # Reading config is safe without the lock — dict.copy() is GIL-protected.
    return agent.config.copy()


@app.post("/api/config")
async def update_config(request: Request):
    data = await request.json()
    # Config writes DO need the lock — they reload internal state.
    with config_lock:
        try:
            agent.save_config(data)
            return agent.config.copy()
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


# ── Chat history ──────────────────────────────────────────────────────────────

@app.get("/api/chat/history")
def get_history(session_id: str = None):
    sid = session_id or agent.session_id
    history = agent.get_history(sid)
    return {"history": history, "session_id": sid}


@app.post("/api/chat/new")
def new_chat():
    """Generate a new random session ID and return it."""
    return {"session_id": str(uuid.uuid4())}


@app.post("/api/chat/clear")
def clear_chat(session_id: str = None):
    """Clear conversation history for a session."""
    sid = session_id or agent.session_id
    agent.clear_history(sid)
    return {"status": "cleared", "session_id": sid}


# ── Chat (non-streaming, backwards-compatible) ────────────────────────────────

@app.post("/api/chat")
async def chat(request: Request):
    """
    Original blocking endpoint — kept for backwards compatibility.
    Runs agent.chat() in the thread pool so the event loop stays free,
    but the HTTP response is still returned only when the agent finishes.
    """
    data = await request.json()
    message = data.get("message", "").strip()
    session_id = data.get("session_id")
    if not message:
        raise HTTPException(status_code=400, detail="Message required")
    try:
        response, total, history = await asyncio.to_thread(
            agent.chat, message, session_id, False
        )
        sid = session_id or agent.session_id
        return {"response": response, "tokens": total, "history": history, "session_id": sid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Chat (SSE streaming) ──────────────────────────────────────────────────────

@app.post("/api/chat/stream")
async def chat_stream(request: Request):
    """
    Streaming endpoint using Server-Sent Events (SSE).

    The agent runs in a worker thread. Each time it calls a tool (or
    produces the final answer) it fires step_callback(), which puts an
    event dict onto a Queue. The async generator below drains that queue
    and forwards each event to the browser in real-time.

    SSE event types emitted:
      thinking    - agent is about to call the LLM          {iteration}
      tool_call   - agent is about to execute a tool        {tool, args}
      tool_result - tool returned                           {tool, result}
      final       - agent produced its final text response  {content}
      error       - an exception was raised                 {content}
      done        - stream is complete (sentinel)           {}
    """
    data = await request.json()
    message = data.get("message", "").strip()
    session_id = data.get("session_id")
    if not message:
        raise HTTPException(status_code=400, detail="Message required")

    # A thread-safe queue shared between the worker thread and this generator.
    step_queue: queue.Queue = queue.Queue()

    def step_callback(event: dict):
        """Called by agent.chat() from the worker thread for each step."""
        step_queue.put(event)

    def run_agent():
        """Blocking agent call — executed in a worker thread."""
        try:
            agent.chat(message, session_id, step_callback=step_callback)
        except Exception as e:
            step_queue.put({"type": "error", "content": str(e)})
        finally:
            step_queue.put(None)  # sentinel: tells the generator we're done

    # Launch the agent in a daemon thread.
    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    async def event_generator():
        loop = asyncio.get_event_loop()
        while True:
            # queue.Queue.get() is blocking — run it in the executor so we
            # don't stall the async event loop while waiting for the next step.
            event = await loop.run_in_executor(None, step_queue.get)

            if event is None:
                # Sentinel — agent finished, close the stream.
                yield _sse_line("done", {})
                break

            yield _sse_line(event["type"], event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx response buffering
            "Connection": "keep-alive",
        },
    )


# ── Tools ─────────────────────────────────────────────────────────────────────

@app.get("/api/tools")
def list_tools():
    return {"tools": list(agent.tools_dict.keys())}


# ── Voice config (system prompt + tools for Gemini Live) ──────────────────────

@app.get("/api/voice-config")
def get_voice_config():
    """
    Returns the full compiled system prompt (identity + soul + profile + memory
    + custom prompt) and tool declarations in Gemini function-calling format so
    the browser can wire them into the Gemini Live session.
    """
    # Build Gemini-format tool declarations from the agent's loaded tools
    gemini_tools = []
    for name, entry in agent.tools_dict.items():
        spec = entry["spec"]
        gemini_tools.append({
            "name": spec.get("name", name),
            "description": spec.get("description", ""),
            "parameters": spec.get("parameters", {}),
        })
    return {
        "system_prompt": agent.system_prompt or "",
        "tools": gemini_tools,
    }


# ── Voice tool execution ───────────────────────────────────────────────────────

@app.post("/api/voice-tool")
async def execute_voice_tool(request: Request):
    """
    Executes a single tool call on behalf of the voice chat session.
    The browser sends { tool: str, args: dict, session_id: str, tool_call_id: str } and receives { result: str }.
    Also saves the tool call and result to the database.
    """
    data = await request.json()
    tool_name = data.get("tool", "")
    args = data.get("args", {})
    session_id = data.get("session_id") or agent.session_id
    tool_call_id = data.get("tool_call_id", "")  # ID from Gemini Live

    if tool_name not in agent.tools_dict:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {tool_name}")

    try:
        # First, save the tool call to database as assistant message with tool_calls
        tool_calls_json = [{
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(args) if args else "{}"
            }
        }]
        
        # Save assistant message with tool call
        agent.memory.add_message(
            session_id=session_id,
            role="assistant",
            content="",  # Gemini Live doesn't provide text content with tool calls
            tool_calls=tool_calls_json
        )
        
        # Execute the tool
        result = await asyncio.to_thread(agent.tools_dict[tool_name]["run"], **args)
        
        # Save tool result to database
        agent.memory.add_message(
            session_id=session_id,
            role="tool",
            content=str(result),
            tool_call_id=tool_call_id,
            tool_name=tool_name
        )
        
        return {"result": str(result)}
    except Exception as e:
        # Still save error result to database
        if 'tool_call_id' in locals() and tool_call_id:
            agent.memory.add_message(
                session_id=session_id,
                role="tool",
                content=f"Tool execution error: {e}",
                tool_call_id=tool_call_id,
                tool_name=tool_name
            )
        raise HTTPException(status_code=500, detail=f"Tool execution error: {e}")


# ── Voice history persistence ──────────────────────────────────────────────────

@app.post("/api/voice-message")
async def save_voice_message(request: Request):
    """
    Persists a completed voice turn to the same SQLite DB used by text chat.
    The browser sends { session_id, user_text, agent_text } after each turn.
    Both messages are tagged with role='user' / role='assistant' exactly as
    agent.chat() does, so voice history appears seamlessly alongside text history.
    """
    data = await request.json()
    session_id = data.get("session_id") or agent.session_id
    user_text  = (data.get("user_text")  or "").strip()
    agent_text = (data.get("agent_text") or "").strip()

    if not user_text and not agent_text:
        raise HTTPException(status_code=400, detail="At least one of user_text or agent_text is required")

    try:
        if user_text:
            agent.memory.add_message(session_id, "user", user_text)
        if agent_text:
            agent.memory.add_message(session_id, "assistant", agent_text)
        return {"status": "saved", "session_id": session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save voice message: {e}")


# ── Static files ──────────────────────────────────────────────────────────────

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
else:
    print(f"[Warning] Static directory not found: {static_dir}")


@app.get("/")
def read_root():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Picolo Web UI. Add static/index.html"}


if __name__ == "__main__":
    #uvicorn.run(app, host="0.0.0.0", port=8000)
    port=agent.config.get("port", 8000)
    host=agent.config.get("host", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)

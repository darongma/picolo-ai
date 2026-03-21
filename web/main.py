"""
Picolo Web UI - FastAPI backend
"""
import sys
import os
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import threading
import uuid

# Add project root to Python path
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJECT_ROOT)

from agent_core import Agent

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

# Global references (will be set in lifespan)
agent = None
agent_lock = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent, agent_lock
    agent = Agent(CONFIG_PATH)
    agent_lock = threading.RLock()
    yield
    # Shutdown: close agent
    if agent:
        agent.close()
    agent = None
    agent_lock = None

app = FastAPI(title="Picolo Web UI", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/logs")
def get_logs(limit: int = 200):
    """Return the last N log lines from in-memory buffer, falling back to file."""
    with agent_lock:
        try:
            logs = agent.get_recent_logs(limit)
            return {"logs": logs}
        except Exception:
            # Fallback to file if in-memory buffer unavailable
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

@app.get("/api/config")
def get_config():
    with agent_lock:
        cfg = agent.config.copy()
        # Hide api_key? We'll return it so user can edit; it's local-only.
        return cfg

@app.post("/api/config")
async def update_config(request: Request):
    data = await request.json()
    with agent_lock:
        try:
            agent.save_config(data)
            return {"status": "updated"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/chat/history")
def get_history(session_id: str = None):
    with agent_lock:
        sid = session_id or agent.session_id
        history = agent.get_history(sid)  # uses token-based limit from config
        return {"history": history, "session_id": sid}

@app.post("/api/chat/new")
def new_chat():
    """Generate a new random session ID and return it."""
    new_sid = str(uuid.uuid4())
    return {"session_id": new_sid}

@app.post("/api/chat/clear")
def clear_chat(session_id: str = None):
    """Clear conversation history for a session."""
    with agent_lock:
        sid = session_id or agent.session_id
        agent.clear_history(sid)
        return {"status": "cleared", "session_id": sid}

@app.post("/api/chat")
async def chat(request: Request):
    data = await request.json()
    message = data.get("message", "").strip()
    session_id = data.get("session_id")
    if not message:
        raise HTTPException(status_code=400, detail="Message required")
    with agent_lock:
        try:
            response, total, history = agent.chat(message, session_id, return_history=False)
            sid = session_id or agent.session_id
            return {"response": response, "tokens":total, "history": history, "session_id": sid}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/tools")
def list_tools():
    with agent_lock:
        tools = list(agent.tools_dict.keys())
        return {"tools": tools}

# Static files
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
    uvicorn.run(app, host="0.0.0.0", port=8000)

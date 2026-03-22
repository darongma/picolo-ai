"""
Agent Core: reusable, thread-safe agent with persistent memory and dynamic tools.
"""
import datetime
import importlib
import json
import logging
import os
import sqlite3
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Any, Optional

from openai import OpenAI

class PrefixedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that names backups as <logname>.<N>.<ext> instead of <logname>.<N>."""
    def rotation_filename(self, default_name: str) -> str:
        # Split into directory and filename
        dir_name, filename = os.path.split(default_name)
        # Split baseFilename to get its parts
        _, base_filename = os.path.split(self.baseFilename)
        if '.' in base_filename:
            name_root, ext = base_filename.rsplit('.', 1)
            # Extract number from filename (expected: base_filename + '.' + number)
            suffix = filename[len(base_filename) + 1:]  # after base_filename and dot
            new_filename = f"{name_root}.{suffix}.{ext}"
        else:
            new_filename = filename
        return os.path.join(dir_name, new_filename) if dir_name else new_filename

# ==================== Memory ====================

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,      -- JSON array of tool_calls (from assistant)
    tool_call_id TEXT,    -- for tool response messages
    tool_name TEXT,       -- tool name for tool responses
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_session_id ON messages(session_id, id);
"""

class Memory:
    """Thread-safe SQLite memory store.

    Each calling thread gets its own sqlite3 connection via threading.local().
    This avoids "database is locked" errors from concurrent writes while
    keeping all sessions in a single on-disk database file.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        # Initialise schema using a temporary connection on the main thread
        conn = sqlite3.connect(db_path)
        conn.executescript(DB_SCHEMA)
        conn.commit()
        conn.close()

    def _conn(self) -> sqlite3.Connection:
        """Return (or create) the sqlite3 connection for the current thread."""
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")   # concurrent readers + writer
            conn.execute("PRAGMA busy_timeout=5000")  # wait up to 5 s on lock
            self._local.conn = conn
        return self._local.conn

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_calls: Optional[List[Dict]] = None,
        tool_call_id: str = None,
        tool_name: str = None
    ) -> str:
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, tool_name)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id,
                tool_name
            )
        )
        conn.commit()
        # Fetch the timestamp of the inserted row (CURRENT_TIMESTAMP)
        cur = conn.execute("SELECT datetime(timestamp, 'localtime') as timestamp FROM messages WHERE rowid = last_insert_rowid()")
        row = cur.fetchone()
        timestamp = row[0] if row else None
        return timestamp

    def clear_history(self, session_id: str):
        """Delete all messages for a given session."""
        conn = self._conn()
        conn.execute(
            "DELETE FROM messages WHERE session_id = ?",
            (session_id,)
        )
        conn.commit()

    def _estimate_message_tokens(self, msg: dict) -> int:
        """Rough token estimation: ~1 token per 4 chars of content + function calls."""
        tokens = 0
        content = msg.get('content', '')
        if content:
            tokens += len(content) // 4
        tool_calls = msg.get('tool_calls', [])
        if tool_calls:
            for tc in tool_calls:
                fn = tc.get('function', {})
                tokens += len(fn.get('name', '')) // 4
                tokens += len(fn.get('arguments', '')) // 4
        tokens += 5  # overhead for role and message structure
        return tokens

    def get_history(self, session_id: str, max_tokens: int) -> List[Dict[str, Any]]:
        """Fetch messages for session, returning the most recent ones that fit within max_tokens."""
        # Fetch a large number of recent messages (newest first)
        # We assume 10000 is enough to fill any reasonable token budget
        cur = self._conn().execute(
            """
            SELECT role, content, tool_calls, tool_call_id, tool_name, datetime(timestamp, 'localtime') as timestamp
            FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT 10000
            """,
            (session_id,)
        )
        rows = cur.fetchall()
        # Convert to message dicts (newest first)
        fetched = []
        for row in rows:
            role, content, tool_calls_json, tool_call_id, tool_name, timestamp = row
            msg: Dict[str, Any] = {"role": role, "content": content, "timestamp": timestamp}
            if tool_calls_json:
                try:
                    msg["tool_calls"] = json.loads(tool_calls_json)
                except json.JSONDecodeError:
                    pass
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
                if tool_name:
                    msg["name"] = tool_name
            fetched.append(msg)

        # Select most recent messages that fit within token budget
        selected = []
        current_tokens = 0
        for msg in fetched:  # iterate newest -> oldest
            est = self._estimate_message_tokens(msg)
            if current_tokens + est <= max_tokens:
                selected.append(msg)
                current_tokens += est
            else:
                # Stop processing if we hit the limit to avoid orphaned tool chains
                break
                
        # Reverse to chronological order (oldest first) for LLM API
        selected.reverse()
        
        # Safety check: if the oldest message is a tool response without its assistant call, remove it
        while selected and selected[0].get("role") == "tool":
            selected.pop(0)
            
        return selected

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None


# ==================== Tool Loading ====================

def load_tools(tools_dir: str) -> Dict[str, Dict]:
    """Load tool modules from tools_dir.

    Supports two patterns:

    1. Single tool module:
       - defines `tool_spec` (dict) and `run` (callable)

    2. Multi-tool module:
       - defines `tool_specs` (list of spec dicts) and `tools` (dict: name -> run)

    Returns: name -> { "spec": tool_spec, "run": callable }
    """
    tools_path = Path(tools_dir)
    if not tools_path.exists():
        print(f"[Warning] Tools directory not found: {tools_dir}. No tools loaded.")
        return {}
    tools: Dict[str, Dict] = {}
    for file in tools_path.glob("*.py"):
        if file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(file.stem, file)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # Pattern 1
            if hasattr(mod, "tool_spec") and hasattr(mod, "run"):
                name = mod.tool_spec["name"]
                tools[name] = {"spec": mod.tool_spec, "run": mod.run}
            # Pattern 2
            elif hasattr(mod, "tool_specs") and hasattr(mod, "tools"):
                for tspec in mod.tool_specs:
                    name = tspec["name"]
                    if name in mod.tools:
                        tools[name] = {"spec": tspec, "run": mod.tools[name]}
                    else:
                        print(f"[Warning] tool spec {name} missing in tools dict in {file.name}")
            else:
                print(f"[Warning] Skipping {file.name}: missing tool_spec/run or tool_specs/tools")
        except Exception as e:
            print(f"[Error] Failed to load tool {file.name}: {e}")
    return tools

def build_openai_tools(tools_dict: Dict[str, Dict]) -> List[Dict]:
    """Convert loaded tools to OpenAI function calling format."""
    return [{"type": "function", "function": t["spec"]} for t in tools_dict.values()]


# ==================== Agent ====================

class Agent:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.lock = threading.RLock()
        self.logger = None  # will be set in _init_components
        with self.lock:
            self.config = self._load_config()
            self._init_components()
            self._log("Agent started")

    def _load_config(self) -> dict:
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Config not found: {self.config_path}")
        with open(self.config_path) as f:
            cfg = json.load(f)
        # Defaults
        cfg.setdefault("base_url", "https://api.openai.com/v1")
        cfg.setdefault("model", "gpt-4o-mini")
        cfg.setdefault("db_path", os.path.join(os.path.dirname(self.config_path), "picolo.db"))
        cfg.setdefault("tools_dir", os.path.join(os.path.dirname(self.config_path), "tools"))
        cfg.setdefault("session_id", "default")
        cfg.setdefault("system_prompt", None)
        # Token-based context limiting (required)
        cfg.setdefault("max_input_tokens", 200000)  # default context window
        cfg.setdefault("log_file", os.path.join(os.path.dirname(self.config_path), "picolo.log"))
        cfg.setdefault("log_max_size", 5 * 1024 * 1024)  # 5 MB
        cfg.setdefault("log_backup_count", 3)
        return cfg

    def _setup_logger(self):
        """Configure rotating file logger for this Agent."""
        # Remove existing handlers if reconfiguring
        if self.logger:
            for h in list(self.logger.handlers):
                self.logger.removeHandler(h)
                h.close()
        else:
            self.logger = logging.getLogger("picolo")
            self.logger.setLevel(logging.INFO)
            self.logger.propagate = False

        log_path = self.config.get("log_file")
        max_size = self.config.get("log_max_size", 5 * 1024 * 1024)
        backup_count = self.config.get("log_backup_count", 3)
        handler = PrefixedRotatingFileHandler(
            log_path,
            maxBytes=max_size,
            backupCount=backup_count,
            encoding="utf-8"
        )
        # Simple formatter: we will pre-format the log line in _log
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger.addHandler(handler)

    def _init_components(self):
        self._setup_logger()
        # In-memory log buffer for fast UI access
        self.recent_logs = deque(maxlen=1000)
        self.memory = Memory(self.config["db_path"])
        self.tools_dir = self.config["tools_dir"]
        self.tools_dict = load_tools(self.tools_dir)
        
        self.openai_tools = build_openai_tools(self.tools_dict) if self.tools_dict else None

        # Determine active provider configuration
        provider_id = self.config.get("provider")
        providers = self.config.get("providers", [])
        active_provider = None
        if provider_id:
            active_provider = next((p for p in providers if p.get("id") == provider_id), None)

        # Resolve API key and base URL: provider-specific overrides top-level
        api_key = self.config.get("api_key", "")
        base_url = self.config.get("base_url", "https://api.openai.com/v1")
        if active_provider:
            if active_provider.get("api_key"):
                api_key = active_provider["api_key"]
            if active_provider.get("base_url"):
                base_url = active_provider["base_url"]

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url
        )
        self.model = self.config.get("model", "gpt-4o-mini")
        # Detect Gemini provider and init google-genai client.
        # The OpenAI SDK strips thought_signatures from Gemini responses, causing
        # 400 errors on multi-turn tool calls. The google-genai SDK preserves them.
        self.gemini_client = None
        self.use_gemini_sdk = False
        if "generativelanguage.googleapis.com" in base_url:
            try:
                from google import genai as _google_genai
                self.gemini_client = _google_genai.Client(api_key=api_key)
                self.use_gemini_sdk = True
            except ImportError:
                pass
        self.session_id = self.config.get("session_id", "default")
        # Build system prompt from identity files + config
        project_root = os.path.dirname(self.config_path)
        identity_parts = []
        for fname in ["IDENTITY.md", "SOUL.md", "PROFILE.md", "MEMORY.md"]:
            fpath = os.path.join(project_root, fname)
            if os.path.exists(fpath):
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                    # Strip YAML frontmatter if present
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            content = parts[2].strip()
                        else:
                            content = content.strip()
                    identity_parts.append(content)
        identity_prompt = "\n\n".join(identity_parts) if identity_parts else ""
        custom_prompt = self.config.get("system_prompt", "")
        if identity_prompt:
            if custom_prompt:
                self.system_prompt = identity_prompt + "\n\n" + custom_prompt
            else:
                self.system_prompt = identity_prompt
        else:
            self.system_prompt = custom_prompt

    def reload_config(self):
        with self.lock:
            self.config = self._load_config()
            self._init_components()

    def reload_tools(self):
        with self.lock:
            self.tools_dict = load_tools(self.config["tools_dir"])
            
            self.openai_tools = build_openai_tools(self.tools_dict) if self.tools_dict else None

    def save_config(self, updates: dict):
        with self.lock:
            # Load current
            current = self._load_config()
            # Merge updates (simple shallow merge; email nested merge)
            for key, value in updates.items():
                if key == "email" and isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key].update(value)
                else:
                    current[key] = value
            # Write back
            with open(self.config_path, "w") as f:
                json.dump(current, f, indent=2)
            # Refresh
            self.config = current
            self._init_components()

    def get_history(self, session_id: str = None, max_tokens: int = None) -> List[Dict]:
        """Return conversation history, limited by token budget."""
        with self.lock:
            sid = session_id or self.session_id
            if max_tokens is None:
                max_tokens = self.config["max_input_tokens"]
            return self.memory.get_history(sid, max_tokens=max_tokens)

    def clear_history(self, session_id: str = None):
        with self.lock:
            sid = session_id or self.session_id
            self.memory.clear_history(sid)
            self._log("History cleared", {"session_id": sid})




    def chat(self, message: str, session_id: str = None, return_history: bool = False, step_callback=None):
        # ── Phase 1: persist user message & build context (needs lock) ──
        with self.lock:
            new_msgs = []
            sid = session_id or self.session_id
            self.memory.add_message(sid, "user", message)
            self._log("User message", {"session_id": sid, "len": len(message)})

            # Build messages from DB using token-based limit
            max_input_tokens = self.config["max_input_tokens"]
            system_estimate = 0
            if self.system_prompt:
                system_estimate = len(self.system_prompt) // 4 + 5
            available = max_input_tokens - system_estimate
            if available <= 0:
                messages = []
            else:
                messages = self.memory.get_history(sid, max_tokens=available)

            # Ensure system prompt
            if self.system_prompt:
                if not any(m.get("role") == "system" for m in messages):
                    system_msg = {"role": "system", "content": self.system_prompt}
                    messages.insert(0, system_msg)

            # Trim if still over token limit (safety margin)
            idx = 1 if messages and messages[0].get("role") == "system" else 0
            total = sum(self.memory._estimate_message_tokens(m) for m in messages)
            while total > max_input_tokens and len(messages) > idx:
                messages.pop(idx)
                total = sum(self.memory._estimate_message_tokens(m) for m in messages)

            # Clean up orphaned tool messages if we sliced a tool chain in half
            while len(messages) > idx and messages[idx].get("role") == "tool":
                messages.pop(idx)

        # ── Phase 2: agent loop (lock released during LLM calls) ──
        max_iterations = self.config.get('max_tool_iterations', 25)
        final_response = None
        gemini_contents = None
        # Per-tool error counter for retry loop prevention
        tool_error_counts = {}
        max_tool_errors = self.config.get('max_tool_errors', 3)

        for iteration in range(max_iterations):
            self._log("LLM request", {"iteration": iteration + 1, "model": self.model, "messages": messages})
            if step_callback:
                step_callback({"type": "thinking", "iteration": iteration + 1})

            # ── LLM call (no lock held) ──────────────────────────────────
            if self.use_gemini_sdk and self.gemini_client:
                try:
                    from google.genai import types as _gt
                    import json as _json, uuid as _uuid

                    if gemini_contents is None:
                        gemini_contents = []
                        system_instruction = None
                        skip_tool_ids = set()  # track tool_call_ids to skip if assistant was skipped

                        for m in messages:
                            role = m["role"]
                            if role == "system":
                                system_instruction = m["content"]
                                continue

                            elif role == "user":
                                gemini_contents.append(_gt.Content(
                                    role="user",
                                    parts=[_gt.Part(text=m["content"] or "")]
                                ))

                            elif role == "assistant":
                                if m.get("content") and not m.get("tool_calls"):
                                    # Plain text response — safe to include
                                    gemini_contents.append(_gt.Content(
                                        role="model",
                                        parts=[_gt.Part(text=m["content"])]
                                    ))
                                elif m.get("tool_calls"):
                                    # Has tool_calls but no thought_signature (came from DB) —
                                    # skip it AND mark its tool result IDs to be skipped too,
                                    # otherwise Gemini sees orphaned tool results with no preceding call
                                    for tc in m["tool_calls"]:
                                        skip_tool_ids.add(tc["id"])

                            elif role == "tool":
                                if m.get("tool_call_id") in skip_tool_ids:
                                    # Orphaned tool result — its assistant call was skipped, drop it
                                    skip_tool_ids.discard(m["tool_call_id"])
                                    continue
                                gemini_contents.append(_gt.Content(
                                    role="user",
                                    parts=[_gt.Part(
                                        function_response=_gt.FunctionResponse(
                                            name=m.get("name") or "tool",
                                            response={"result": m["content"] or ""}
                                        )
                                    )]
                                ))


                    gemini_tools = None
                    if self.openai_tools:
                        fn_decls = [
                            _gt.FunctionDeclaration(
                                name=t["function"]["name"],
                                description=t["function"].get("description", ""),
                                parameters=t["function"].get("parameters")
                            ) for t in self.openai_tools
                        ]
                        gemini_tools = [_gt.Tool(function_declarations=fn_decls)]

                    gemini_config = _gt.GenerateContentConfig(
                        system_instruction=system_instruction if iteration == 0 else None,
                        tools=gemini_tools,
                        automatic_function_calling=_gt.AutomaticFunctionCallingConfig(disable=True),
                    )
                    self._log("Gemini request", {"iteration": iteration + 1, "contents_len": len(gemini_contents)})
                    gemini_response = self.gemini_client.models.generate_content(
                        model=self.model,
                        contents=gemini_contents,
                        config=gemini_config
                    )
                    raw_model_content = gemini_response.candidates[0].content
                    gemini_contents.append(raw_model_content)

                    class _TC:
                        def __init__(self, id_, name, args_str):
                            self.id = id_; self.type = "function"
                            class _F: pass
                            self.function = _F()
                            self.function.name = name
                            self.function.arguments = args_str
                    class _AM: pass
                    assistant_msg = _AM()
                    assistant_msg.content = None
                    assistant_msg.tool_calls = None
                    fn_calls = gemini_response.function_calls
                    if fn_calls:
                        assistant_msg.tool_calls = [
                            _TC(str(_uuid.uuid4())[:8], fc.name, _json.dumps(dict(fc.args)))
                            for fc in fn_calls
                        ]
                    else:
                        assistant_msg.content = gemini_response.text or ""
                except Exception as e:
                    final_response = f"Gemini API error: ❗ {e}"
                    break
            else:
                api_messages = []
                for m in messages:
                    api_msg = {"role": m["role"], "content": m["content"] or ""}
                    if "tool_calls" in m:
                        api_msg["tool_calls"] = m["tool_calls"]
                    if "tool_call_id" in m:
                        api_msg["tool_call_id"] = m["tool_call_id"]
                    if "name" in m and m["role"] != "tool":
                        api_msg["name"] = m["name"]
                    api_messages.append(api_msg)
                try:
                    response = self.client.chat.completions.create(
                        model=self.model,
                        messages=api_messages,
                        tools=self.openai_tools,
                        tool_choice="auto" if self.openai_tools else None,
                        timeout=self.config.get('llm_timeout_seconds', 60)
                    )
                except Exception as e:
                    final_response = f"OpenAI API error: ❗ {e}"
                    break
                assistant_msg = response.choices[0].message

            # ── Persist assistant message (re-acquire lock) ──────────────
            tool_calls_json = None
            if assistant_msg.tool_calls:
                tool_calls_json = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    }
                    for tc in assistant_msg.tool_calls
                ]

            with self.lock:
                assistant_ts = self.memory.add_message(
                    sid,
                    role="assistant",
                    content=assistant_msg.content or "",
                    tool_calls=tool_calls_json
                )

            assistant_dict = {
                "role": "assistant",
                "content": assistant_msg.content or "",
                "timestamp": assistant_ts,
            }
            if tool_calls_json:
                assistant_dict["tool_calls"] = tool_calls_json
            messages.append(assistant_dict)
            new_msgs.append(assistant_dict)

            # ── Tool execution branch ────────────────────────────────────
            if assistant_msg.tool_calls:
                abort_loop = False
                for tc in assistant_msg.tool_calls:
                    tool_name = tc.function.name
                    result = None

                    # Check if this tool has exceeded its error budget
                    if tool_error_counts.get(tool_name, 0) >= max_tool_errors:
                        result = f"Error: tool '{tool_name}' has failed {max_tool_errors} times and has been disabled for this request."
                        abort_loop = True
                    else:
                        try:
                            args = json.loads(tc.function.arguments)
                        except json.JSONDecodeError as e:
                            result = f"Error parsing tool arguments: {e}"
                            tool_error_counts[tool_name] = tool_error_counts.get(tool_name, 0) + 1

                        if result is None:
                            if step_callback:
                                step_callback({
                                    "type": "tool_call",
                                    "tool": tool_name,
                                    "args": tc.function.arguments
                                })
                            if tool_name in self.tools_dict:
                                try:
                                    result = self.tools_dict[tool_name]["run"](**args)
                                except Exception as e:
                                    result = f"Tool execution error: {e}"
                                    tool_error_counts[tool_name] = tool_error_counts.get(tool_name, 0) + 1
                                    # Check threshold immediately after incrementing
                                    if tool_error_counts[tool_name] >= max_tool_errors:
                                        result += f" (tool disabled after {max_tool_errors} failures)"
                                        abort_loop = True
                            else:
                                result = f"Error: unknown tool '{tool_name}'"
                                # Unknown tool counts as a permanent error — disable immediately
                                tool_error_counts[tool_name] = max_tool_errors
                                abort_loop = True

                    if step_callback:
                        step_callback({
                            "type": "tool_result",
                            "tool": tool_name,
                            "args": tc.function.arguments,
                            "result": str(result)
                        })

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(result),
                        "name": tool_name,
                        "args": tc.function.arguments
                    }

                    with self.lock:
                        tool_ts = self.memory.add_message(
                            sid,
                            role="tool",
                            content=str(result),
                            tool_call_id=tc.id,
                            tool_name=tool_name
                        )

                    tool_msg["timestamp"] = tool_ts
                    messages.append(tool_msg)
                    new_msgs.append(tool_msg)

                    if self.use_gemini_sdk and gemini_contents is not None:
                        try:
                            from google.genai import types as _gt2
                            gemini_contents.append(_gt2.Content(
                                role="user",
                                parts=[_gt2.Part(
                                    function_response=_gt2.FunctionResponse(
                                        name=tool_name,
                                        response={"result": str(result)}
                                    )
                                )]
                            ))
                        except Exception:
                            pass

                if abort_loop:
                    # Send one final LLM call so it can summarize/explain the failure
                    # naturally rather than returning a raw error string to the user.
                    # We do this by breaking with final_response=None and letting the
                    # loop continue — but we've already appended the error tool result,
                    # so the next iteration will get a non-tool response.
                    continue

                continue

            else:
                # ── Final answer ─────────────────────────────────────────
                final_response = assistant_msg.content or f"⚠️ LLM assistant_msg.content is {repr(assistant_msg.content)}"
                break

        # ── Phase 3: finalise ────────────────────────────────────────────
        if final_response is None:
            final_response = "❌ Error: Maximum tool call iterations exceeded. Please try a simpler request."

        # Recompute total tokens accurately after the full agent loop
        total = sum(self.memory._estimate_message_tokens(m) for m in messages)

        self._log("Assistant response", {"session_id": sid, "len": len(final_response)})

        if return_history:
            clean_messages = self._sanitize_for_log(messages)
        else:
            clean_messages = self._sanitize_for_log(new_msgs)

        # Fire the final step_callback here, after clean_messages is ready,
        # so the SSE stream can include the new messages and the frontend
        # never needs to re-fetch history.
        if step_callback:
            step_callback({"type": "final", "content": final_response, "history": clean_messages, "tokens": total})

        return final_response, total, clean_messages















    def _sanitize_for_log(self, obj):
        """Convert non‑serializable OpenAI message objects into plain dicts."""
        if isinstance(obj, list):
            return [self._sanitize_for_log(item) for item in obj]
        if isinstance(obj, dict):
            return {k: self._sanitize_for_log(v) for k, v in obj.items()}
        # Check for OpenAI message objects (has role, content, tool_calls attrs)
        if hasattr(obj, "role") and hasattr(obj, "content"):
            d = {"role": obj.role, "content": obj.content}
            if hasattr(obj, "tool_calls") and obj.tool_calls:
                d["tool_calls"] = []
                for tc in obj.tool_calls:
                    d["tool_calls"].append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    })
            return d
        return obj

    def get_recent_logs(self, limit: int = 200) -> List[str]:
        """Return the last N log lines from the in-memory buffer."""
        with self.lock:
            # deque slicing not supported; convert to list and slice
            return list(self.recent_logs)[-limit:]

    def _log(self, event: str, extra: dict = None):
        """Write a log line to the rotating file logger and in-memory buffer."""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{timestamp}] {event}"
            if extra:
                sanitized = self._sanitize_for_log(extra)
                line += f" {json.dumps(sanitized, ensure_ascii=False)}"
            if self.logger:
                self.logger.info(line)
            # Store in recent logs buffer (thread-safe: _log called within self.lock)
            self.recent_logs.append(line)
        except Exception:
            pass

    def close(self):
        with self.lock:
            self.memory.close()
            if self.logger:
                for h in list(self.logger.handlers):
                    h.close()
                    self.logger.removeHandler(h)
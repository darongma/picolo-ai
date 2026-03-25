"""
Shell tool: execute a shell command.
Adds the ability for the agent to run commands on the host system.
"""
import os
import json
import subprocess

# Load configuration
CONFIG = {}
_config_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
try:
    with open(_config_path) as f:
        CONFIG = json.load(f)
except Exception:
    CONFIG = {}

# Maximum characters kept from stdout/stderr before truncating.
# Prevents runaway commands from flooding the agent's context window.
_DEFAULT_MAX_OUTPUT = 20_000

tool_spec = {
    "name": "shell",
    "description": (
        "Execute a shell command and return its output. "
        "Use for running scripts, package management, copy/move/delete file operations, "
        "and system queries. "
        "Do NOT use for reading or editing file contents — use the file_edit tool instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute."
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds. Defaults to config.shell_timeout_seconds (or 30)."
            },
            "workdir": {
                "type": "string",
                "description": "Working directory to run the command in. Defaults to the current directory."
            }
        },
        "required": ["command"]
    }
}


def run(command: str, timeout: int = None, workdir: str = None) -> str:
    """Run a shell command safely and capture output."""
    if timeout is None:
        timeout = CONFIG.get("shell_timeout_seconds", 30)

    max_output = CONFIG.get("shell_max_output_chars", _DEFAULT_MAX_OUTPUT)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir or None,
        )

        def _format(label: str, text: str) -> str:
            text = text.strip()
            if not text:
                return ""
            if len(text) > max_output:
                kept = text[:max_output]
                dropped = len(text) - max_output
                text = f"{kept}\n[... {dropped} characters truncated ...]"
            return f"[{label}]\n{text}"

        parts = [
            f"$ {command}",
            f"[exit code: {result.returncode}]",
        ]
        if workdir:
            parts.append(f"[workdir: {workdir}]")

        stdout_block = _format("stdout", result.stdout)
        stderr_block = _format("stderr", result.stderr)

        if stdout_block:
            parts.append(stdout_block)
        if stderr_block:
            parts.append(stderr_block)
        if not stdout_block and not stderr_block:
            parts.append("(no output)")

        return "\n".join(parts)

    except subprocess.TimeoutExpired:
        return f"$ {command}\n[Error: command timed out after {timeout} seconds]"
    except Exception as e:
        return f"$ {command}\n[Error: {e}]"
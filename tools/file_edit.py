"""
file_edit — reliable file editing with LLM-forgiving whitespace elasticity.

Operations:
  read        : return the exact file contents
  write       : overwrite or create a file
  str_replace : replace a unique block of text (forgiving of whitespace/newlines)
"""

import difflib
import os
import shutil
import stat
import tempfile
import re

tool_spec = {
    "name": "file_edit",
    "description": (
        "Read and edit files reliably. "
        "Always call read before any edit. For str_replace, copy old_content from the result. "
        "The tool is forgiving of minor whitespace and newline differences, so focus on matching the code structure."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read", "write", "str_replace"],
                "description": (
                    "read        – return the exact file contents.\n"
                    "write       – overwrite or create a file with new_content.\n"
                    "str_replace – replace old_content with new_content. "
                    "old_content must be structurally unique in the file. "
                    "To insert: include the anchor line in old_content and repeat it in new_content with your addition. "
                    "To delete: set new_content to empty string."
                ),
            },
            "path": {
                "type": "string",
                "description": "Path to the file.",
            },
            "old_content": {
                "type": "string",
                "description": "Text to replace. Whitespace/newlines do not need to be byte-perfect.",
            },
            "new_content": {
                "type": "string",
                "description": "Replacement text. Use empty string to delete.",
            },
        },
        "required": ["operation", "path"],
    },
}

# ── helpers ───────────────────────────────────────────────────────────────────

def _read(path):
    if not os.path.exists(path):
        return None, f"[Error: file not found: {path}]"
    with open(path, "r", encoding="utf-8") as f:
        return f.read(), None

def _atomic_write(path, content):
    abs_path = os.path.abspath(path)
    dir_ = os.path.dirname(abs_path) or "."
    os.makedirs(dir_, exist_ok=True)

    original_stat = os.stat(abs_path) if os.path.exists(abs_path) else None

    fd, tmp = tempfile.mkstemp(dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if original_stat:
            os.chmod(tmp, stat.S_IMODE(original_stat.st_mode))
            try:
                os.chown(tmp, original_stat.st_uid, original_stat.st_gid)
            except (AttributeError, PermissionError):
                pass
        else:
            os.chmod(tmp, 0o666)
        shutil.move(tmp, abs_path)
        if original_stat:
            os.chmod(abs_path, stat.S_IMODE(original_stat.st_mode))
        else:
            os.chmod(abs_path, 0o666)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _closest_hint(needle, haystack):
    first_line = (needle.splitlines() or [""])[0].strip()
    candidates = [l.strip() for l in haystack.splitlines()]
    matches = difflib.get_close_matches(first_line, candidates, n=3, cutoff=0.4)
    hints = []
    for m in matches:
        for line in haystack.splitlines():
            if line.strip() == m:
                hints.append(f"  similar line: {line!r}")
                break
    return "\n".join(hints)

def _normalize_whitespace(text):
    """Converts all whitespace (spaces, tabs, newlines) to a single space."""
    return re.sub(r'\s+', ' ', text.strip())

def _elastic_replace(text, old_content, new_content):
    """
    Finds old_content in text even if whitespace/newlines don't match perfectly.
    """
    if text.count(old_content) == 1:
        return text.replace(old_content, new_content, 1), None

    norm_old = _normalize_whitespace(old_content)
    if not norm_old:
        return None, "old_content contains only whitespace."

    escaped_words = [re.escape(word) for word in norm_old.split(' ')]
    flexible_pattern = r'\s*'.join(escaped_words)
    
    try:
        matches = list(re.finditer(flexible_pattern, text))
    except re.error:
        return None, "Regex compilation failed during flexible matching."
    
    if len(matches) == 0:
        return None, "Not found."
    if len(matches) > 1:
        return None, f"Found {len(matches)} structural matches. Include more surrounding lines to make it unique."
        
    match = matches[0]
    new_text = text[:match.start()] + new_content + text[match.end():]
    return new_text, None

# ── public run() ──────────────────────────────────────────────────────────────

def run(operation, path, old_content=None, new_content=None):
    if operation == "read":
        text, err = _read(path)
        return err if err else text

    if operation == "write":
        if new_content is None:
            return "[Error: new_content is required]"
        _atomic_write(path, new_content)
        return f"[OK: {path} written]"

    if operation == "str_replace":
        if old_content is None or new_content is None:
            return "[Error: old_content and new_content are required]"
        
        text, err = _read(path)
        if err:
            return err

        new_text, replace_err = _elastic_replace(text, old_content, new_content)
        
        if replace_err:
            hint = _closest_hint(old_content, text)
            msg = f"[Error: {replace_err} in {path}]"
            if hint and "Not found" in replace_err:
                msg += f"\n{hint}"
            msg += f"\nCall read on {path!r} and copy the block verbatim, then retry."
            return msg

        _atomic_write(path, new_text)
        return f"[OK: str_replace applied elastically to {path}]"

    return f"[Error: unknown operation '{operation}']"
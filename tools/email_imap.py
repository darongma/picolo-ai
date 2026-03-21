#!/usr/bin/env python3
"""
IMAP email checking tool for Picolo.
Read emails from an IMAP server (e.g., IONOS).
"""

import os
import json
import logging
import imaplib
import email
from email.header import decode_header
from datetime import datetime

logger = logging.getLogger("email_imap")


def _get_imap_config():
    # Locate config.json in the project root (one level up from tools/)
    config_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
    try:
        with open(config_path, "r") as f:
            cfg = json.load(f)
    except Exception as e:
        raise ValueError(f"Could not load config.json: {e}")
    email_cfg = cfg.get("email", {})
    # Required keys
    for k in ["imap_server", "imap_port", "username", "password"]:
        if k not in email_cfg:
            raise ValueError(f"Missing IMAP email config key: {k}")
    imap_server = email_cfg["imap_server"]
    imap_port = int(email_cfg["imap_port"])
    username = email_cfg["username"]
    password = email_cfg["password"]
    use_ssl = email_cfg.get("imap_use_ssl", True)
    return imap_server, imap_port, username, password, use_ssl


def _decode_header_str(hdr):
    if hdr is None:
        return ""
    decoded = decode_header(hdr)
    parts = []
    for part, enc in decoded:
        if isinstance(part, bytes):
            try:
                if enc:
                    parts.append(part.decode(enc, errors='replace'))
                else:
                    parts.append(part.decode('utf-8', errors='replace'))
            except Exception:
                parts.append(part.decode('latin-1', errors='replace'))
        else:
            parts.append(part)
    return ''.join(parts)


# Tool specs
tool_specs = [
    {
        "name": "email_list",
        "description": "List recent emails from the INBOX. Optionally filter by sender or subject. Returns list with uid, from, subject, date.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum number of emails to return (default 10)."},
                "search": {"type": "string", "description": "Optional search term to match in sender or subject."}
            },
            "required": []
        }
    },
    {
        "name": "email_read",
        "description": "Read a specific email by its unique identifier (uid). Returns full content including body and headers.",
        "parameters": {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "The UID of the email to fetch (as returned by email_list)."}
            },
            "required": ["uid"]
        }
    }
]

# Tool functions
def email_list(limit: int = None, search: str = None):
    if limit is None:
        config_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            limit = cfg.get('email_imap_default_limit', 10)
        except Exception:
            limit = 10
    try:
        server, port, user, pwd, use_ssl = _get_imap_config()
        if use_ssl:
            mail = imaplib.IMAP4_SSL(server, port)
        else:
            mail = imaplib.IMAP4(server, port)
        mail.login(user, pwd)
        mail.select("INBOX")
        if search:
            # Simple search: match FROM or SUBJECT
            search_bytes = f'(OR FROM "{search}" SUBJECT "{search}")'.encode('utf-8')
            _, data = mail.search(None, search_bytes)
        else:
            _, data = mail.search(None, "ALL")
        email_ids = data[0].split()
        # Most recent last; take last 'limit'
        recent_ids = email_ids[-limit:] if limit else email_ids[-10:]
        result = []
        for eid in recent_ids:
            eid_str = eid.decode()
            _, msg_data = mail.fetch(eid_str, "(RFC822.HEADER)")
            raw_header = msg_data[0][1]
            msg = email.message_from_bytes(raw_header)
            subject = _decode_header_str(msg.get("Subject"))
            from_ = _decode_header_str(msg.get("From"))
            date_ = msg.get("Date")
            result.append({
                "uid": eid_str,
                "from": from_,
                "subject": subject,
                "date": date_
            })
        mail.logout()
        return result
    except Exception as e:
        logger.exception("email_list failed")
        return f"Error: {e}"


def email_read(uid):
    try:
        server, port, user, pwd, use_ssl = _get_imap_config()
        if use_ssl:
            mail = imaplib.IMAP4_SSL(server, port)
        else:
            mail = imaplib.IMAP4(server, port)
        mail.login(user, pwd)
        mail.select("INBOX")
        _, msg_data = mail.fetch(uid, "(RFC822)")
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)
        subject = _decode_header_str(msg.get("Subject"))
        from_ = _decode_header_str(msg.get("From"))
        to_ = _decode_header_str(msg.get("To"))
        date_ = msg.get("Date")
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    try:
                        body_bytes = part.get_payload(decode=True)
                        body = body_bytes.decode('utf-8', errors='replace')
                    except Exception:
                        body = "(Unable to decode body)"
                    break
        else:
            try:
                body_bytes = msg.get_payload(decode=True)
                body = body_bytes.decode('utf-8', errors='replace')
            except Exception:
                body = "(Unable to decode body)"
        mail.logout()
        return {
            "uid": uid,
            "from": from_,
            "to": to_,
            "date": date_,
            "subject": subject,
            "body": body
        }
    except Exception as e:
        logger.exception("email_read failed")
        return f"Error: {e}"


tools = {
    "email_list": email_list,
    "email_read": email_read,
}

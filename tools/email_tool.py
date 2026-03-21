#!/usr/bin/env python3
"""
Email tool for Picolo: send emails via SMTP.
Configure via config.json -> email: { smtp_server, smtp_port, username, password }.
"""

import os
import json
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

logger = logging.getLogger("email_tool")


def _get_email_config():
    # Locate config.json in the project root (one level up from tools/)
    config_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
    try:
        with open(config_path, "r") as f:
            cfg = json.load(f)
    except Exception as e:
        raise ValueError(f"Could not load config.json: {e}")
    email_cfg = cfg.get("email", {})
    if not email_cfg:
        raise ValueError("Email configuration missing in config.json")
    required = ["smtp_server", "smtp_port", "username", "password"]
    missing = [k for k in required if k not in email_cfg]
    if missing:
        raise ValueError(f"Missing email config keys: {missing}")
    return email_cfg


def _connect_smtp():
    cfg = _get_email_config()
    server = smtplib.SMTP(cfg["smtp_server"], int(cfg["smtp_port"]))
    server.starttls()
    server.login(cfg["username"], cfg["password"])
    return server


# Tool: email_send
tool_specs = [
    {
        "name": "email_send",
        "description": (
            "Send an email via the configured SMTP server. "
            "You can specify recipients, subject, body, and optional attachments."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of recipient email addresses",
                },
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Plain text body of the email"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths to attach (optional)",
                },
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC recipients (optional)",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BCC recipients (optional)",
                },
            },
            "required": ["to", "subject", "body"],
        }
    }
]

def email_send(to, subject, body, attachments=None, cc=None, bcc=None):
    """
    Send an email.
    Parameters:
      to: list of email strings
      subject: string
      body: string
      attachments: optional list of file paths
      cc: optional list of email strings
      bcc: optional list of email strings
    Returns: status string
    """
    try:
        cfg_email = _get_email_config()
        # Build message
        msg = MIMEMultipart()
        msg["From"] = cfg_email["username"]
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            # BCC recipients are added only as recipients, not in headers
            pass

        msg.attach(MIMEText(body, "plain"))

        # Attachments
        if attachments:
            for file_path in attachments:
                if not os.path.isfile(file_path):
                    logger.warning(f"Attachment not found, skipping: {file_path}")
                    continue
                with open(file_path, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                filename = os.path.basename(file_path)
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{filename}"',
                )
                msg.attach(part)

        # Connect and send
        server = _connect_smtp()
        # Combine all recipients (To, CC, BCC) for envelope
        all_recipients = list(to)
        if cc:
            all_recipients.extend(cc)
        if bcc:
            all_recipients.extend(bcc)

        server.sendmail(cfg_email["username"], all_recipients, msg.as_string())
        server.quit()
        return f"Email sent to {', '.join(to)}"
    except Exception as e:
        logger.exception("Failed to send email")
        return f"Error: {e}"


tools = {
    "email_send": email_send,
}

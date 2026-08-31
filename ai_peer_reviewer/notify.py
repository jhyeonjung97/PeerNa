"""Tell the user a review finished.

A run takes several minutes, which is long enough to walk away from. Two
channels, both optional and both failing quietly: a desktop notification for
when you are at the machine, and an email for when you are not.
"""

from __future__ import annotations

import os
import smtplib
import subprocess
from email.message import EmailMessage

#: Set in ~/.config/ai-peer-reviewer/.env to turn email on.
EMAIL_TO = "AI_PEER_REVIEWER_EMAIL"
SMTP_USER = "SMTP_USER"
SMTP_PASSWORD = "SMTP_PASSWORD"
SMTP_HOST = "SMTP_HOST"

#: Who the mail claims to be from. With a personal mailbox this is the same as
#: the login, which is why it went unnoticed. A sending service is different:
#: Resend logs you in as the literal string "resend" and takes the API key as
#: the password, so the username is not an address at all and a message built
#: from it is rejected before it leaves.
SMTP_FROM = "SMTP_FROM"


def announce(subject: str, body: str, subtitle: str = "") -> list[str]:
    """Send what is configured. Returns the channels that actually went out."""
    sent = []
    if _desktop(subject, body, subtitle):
        sent.append("desktop")
    if _email(subject, body):
        sent.append("email")
    return sent


def _desktop(title: str, body: str, subtitle: str = "") -> bool:
    script = f"display notification {_quote(body)} with title {_quote(title)}"
    if subtitle:
        script += f" subtitle {_quote(subtitle)}"
    try:
        subprocess.run(["osascript", "-e", script],
                       capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


def _email(subject: str, body: str) -> bool:
    to = os.environ.get(EMAIL_TO)
    user = os.environ.get(SMTP_USER) or to
    password = os.environ.get(SMTP_PASSWORD)
    sender = os.environ.get(SMTP_FROM) or user
    if not (to and user and password):
        return False

    host = os.environ.get(SMTP_HOST, "smtp.gmail.com")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    message.set_content(body)

    try:
        with smtplib.SMTP_SSL(host, 465, timeout=30) as server:
            server.login(user, password)
            server.send_message(message)
        return True
    except Exception:
        return False


def _quote(text: str) -> str:
    """AppleScript string literal — a stray quote would break the -e argument."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"')[:400] + '"'

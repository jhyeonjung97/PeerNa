"""Sign in with Google, so that a deployed service knows who is asking.

Without this a public address is an open bar: anyone who knows it can spend the
key every review is billed to, and the review history is one list nobody can be
excluded from. Both of those are the same missing fact — who this is.

Google rather than emailed links, for one reason that decided it: a magic link
has to be delivered, delivery needs a sending service, and a sending service
will not mail strangers until you have verified a domain you own. Google needs
no domain at all. The redirect can point at whatever address the service already
answers on.

No new dependency. The authorization-code flow is two HTTP requests, and a
signed cookie is `hmac`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT_ID = "GOOGLE_CLIENT_ID"
CLIENT_SECRET = "GOOGLE_CLIENT_SECRET"

AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"

#: Only what is needed to know who someone is. Both are non-sensitive, which is
#: what keeps this out of Google's review queue for sensitive scopes.
SCOPES = "openid email"

COOKIE = "peerna_session"

#: How long a sign-in lasts. Long enough not to interrupt a review and to
#: survive closing the laptop; short enough that a borrowed browser forgets.
SESSION_DAYS = 30

#: Where the key that signs sessions is kept. Generated on first use rather than
#: configured, because there is nothing an operator would want to choose here —
#: and a secret that must be set is a secret that ends up in a repository.
SECRET_PATH = Path.home() / ".cache" / "ai-peer-reviewer" / "session.key"


def configured() -> bool:
    return bool(os.environ.get(CLIENT_ID) and os.environ.get(CLIENT_SECRET))


def _secret() -> bytes:
    if SECRET_PATH.is_file():
        return SECRET_PATH.read_bytes()
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    SECRET_PATH.write_bytes(key)
    SECRET_PATH.chmod(0o600)
    return key


def _sign(payload: bytes) -> str:
    mac = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(mac)}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(email: str) -> str:
    """A cookie value naming this person, signed so it cannot be edited."""
    payload = json.dumps(
        {"email": email, "until": int(time.time()) + SESSION_DAYS * 86400}
    ).encode()
    return _sign(payload)


def read(cookie: str | None) -> str | None:
    """The email a cookie names, or None if it is absent, forged or expired."""
    if not cookie or "." not in cookie:
        return None
    body, mac = cookie.rsplit(".", 1)
    try:
        payload = _unb64(body)
        expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(mac), expected):
            return None
        claim = json.loads(payload)
    except Exception:
        return None
    if claim.get("until", 0) < time.time():
        return None
    email = claim.get("email")
    return email if isinstance(email, str) and email else None


def login_url(redirect_uri: str, state: str) -> str:
    return AUTHORIZE + "?" + urllib.parse.urlencode({
        "client_id": os.environ[CLIENT_ID],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        # A refresh token would let this act for someone while they are away.
        # It never needs to: the only question ever asked of Google is who this
        # is, once, at sign-in.
        "access_type": "online",
        "prompt": "select_account",
    })


def exchange(code: str, redirect_uri: str) -> str | None:
    """Trade the one-time code for the signed-in address."""
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": os.environ[CLIENT_ID],
        "client_secret": os.environ[CLIENT_SECRET],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    request = urllib.request.Request(
        TOKEN, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.loads(response.read())
    except Exception:
        return None

    # The id_token is a JWT that Google has just handed us over TLS, in direct
    # response to a code only we could have redeemed. Its signature is worth
    # checking when a token arrives from somewhere else; here the transport is
    # the proof, and verifying it would mean fetching and caching Google's keys
    # for no gain.
    raw = token.get("id_token") or ""
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(_unb64(parts[1]))
    except Exception:
        return None
    if not claims.get("email_verified"):
        return None
    email = claims.get("email")
    return email if isinstance(email, str) and email else None

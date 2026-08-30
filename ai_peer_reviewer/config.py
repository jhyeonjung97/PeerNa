"""Where the API key comes from.

Precedence: the environment wins, then a config-directory .env, then a .env
next to the project. The last of those is supported because it is what people
expect, and warned about because this project may well be sitting inside a
synced cloud folder — where a .env is uploaded like any other file. `.gitignore`
does not prevent that; it only hides the file from git.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "ai-peer-reviewer"
CONFIG_ENV = CONFIG_DIR / ".env"

KEY_NAME = "ANTHROPIC_API_KEY"
OPENAI_KEY_NAME = "OPENAI_API_KEY"
ROUTER_KEY_NAME = "MONO_ROURTER_API_KEY"
MODEL_NAME = "AI_PEER_REVIEWER_MODEL"

#: Set when the account has a zero-retention agreement with its provider. The
#: tool cannot verify this — it is a contract, not a setting — so it is taken on
#: the operator's word and used to refuse models that cannot run under one.
ZERO_RETENTION = "AI_PEER_REVIEWER_ZERO_RETENTION"

#: Settings the .env file may set. Anything else in the file is ignored.
NOTIFY_KEYS = ("AI_PEER_REVIEWER_EMAIL", "SMTP_USER", "SMTP_PASSWORD", "SMTP_HOST")
RECOGNISED = (KEY_NAME, OPENAI_KEY_NAME, ROUTER_KEY_NAME, MODEL_NAME,
              ZERO_RETENTION, *NOTIFY_KEYS)

#: Path fragments that mean a directory is synced to somebody's cloud.
CLOUD_MARKERS = (
    "google drive",
    "googledrive",
    "/my drive/",
    "dropbox",
    "onedrive",
    "library/mobile documents",  # iCloud Drive
)


def load() -> None:
    """Merge settings from the first .env that has any, without overriding the
    real environment. Call this once, before reading any setting."""
    for candidate in (CONFIG_ENV, Path.cwd() / ".env"):
        if not candidate.is_file():
            continue
        values = {
            name: value
            for name, value in _parse_env(candidate).items()
            if name in RECOGNISED and value
        }
        if not values:
            continue
        holds_secret = any(name.endswith("API_KEY") for name in values)
        if holds_secret and _is_cloud_synced(candidate):
            print(
                f"WARNING: {candidate} is inside a cloud-synced folder, so your "
                f"API key(s) are being uploaded and will travel with anyone you share "
                f"that folder with.\n"
                f"         Move it to {CONFIG_ENV} instead.\n",
                file=sys.stderr,
            )
        for name, value in values.items():
            os.environ.setdefault(name, value)
        return


def has_credentials(provider: str = "anthropic") -> bool:
    if provider == "openai":
        return bool(os.environ.get(OPENAI_KEY_NAME))
    if os.environ.get(KEY_NAME) or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # An `ant auth login` profile also works; the SDK finds it without help.
    return (Path.home() / ".config" / "anthropic").exists()


def configured_model() -> str | None:
    return os.environ.get(MODEL_NAME) or None


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        value = value.strip().strip("'\"")
        if name:
            values[name] = value
    return values


def _is_cloud_synced(path: Path) -> bool:
    haystack = str(path.resolve()).lower()
    return any(marker in haystack for marker in CLOUD_MARKERS)


def save_key(provider: str, key: str) -> None:
    """Write one provider's key into the config file, leaving the rest alone.

    The file is rewritten in place rather than appended to, so setting a key
    twice replaces it instead of leaving a stale line that may or may not win.
    Permissions are reasserted every time: a secret written world-readable is
    still a leaked secret.
    """
    name = OPENAI_KEY_NAME if provider == "openai" else KEY_NAME
    key = key.strip()
    if not key:
        raise ValueError("The key is empty.")
    if any(c.isspace() for c in key):
        raise ValueError("The key contains whitespace — it may be truncated or wrapped.")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    replaced = False
    if CONFIG_ENV.is_file():
        for line in CONFIG_ENV.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{name}=") or stripped.startswith(f"export {name}="):
                if not replaced:
                    lines.append(f"{name}={key}")
                    replaced = True
                continue
            lines.append(line)
    if not replaced:
        lines.append(f"{name}={key}")

    CONFIG_ENV.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    CONFIG_ENV.chmod(0o600)
    os.environ[name] = key


def key_hint(provider: str) -> str | None:
    """A few trailing characters, enough to tell two keys apart. Never the key."""
    name = OPENAI_KEY_NAME if provider == "openai" else KEY_NAME
    value = os.environ.get(name) or ""
    return f"…{value[-4:]}" if len(value) >= 8 else None


def write_key_template() -> Path:
    """Create the config-directory .env with restrictive permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_ENV.exists():
        CONFIG_ENV.write_text(
            f"{KEY_NAME}=\n{OPENAI_KEY_NAME}=\n", encoding="utf-8"
        )
    CONFIG_ENV.chmod(0o600)
    return CONFIG_ENV


def zero_retention() -> bool:
    """Whether the operator has declared a zero-retention agreement."""
    return (os.environ.get(ZERO_RETENTION) or "").strip().lower() in ("1", "true", "yes")

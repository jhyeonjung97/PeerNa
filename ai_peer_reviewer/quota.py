"""How many reviews an account has had, and how many are free.

Kept apart from the job history on purpose. That is capped at fifty files and
sweeps the oldest, so counting finished jobs would quietly hand somebody a fresh
allowance once enough other people had used the service — the quota would leak
in exactly the conditions that make a quota matter.

Only completed reviews count. A run that failed, or that a deploy interrupted,
was not a review anybody received, and charging for it would be charging for our
own fault. That leaves a way to run indefinitely by abandoning every review just
before it finishes; it is a worse thing to defend against than the alternative,
which is invoicing people for our outages.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

#: Reviews an account gets before it has to pay.
FREE_REVIEWS = int(os.environ.get("AI_PEER_REVIEWER_FREE_REVIEWS") or 3)

PATH = Path.home() / ".cache" / "ai-peer-reviewer" / "quota.json"

_LOCK = threading.Lock()

#: Nobody signs in to reach their own laptop, and nobody should be rationed on
#: it either. The web path uses this same name for an unauthenticated local run.
EXEMPT = {"local", None, ""}


def _read() -> dict[str, int]:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}


def used(email: str | None) -> int:
    if email in EXEMPT:
        return 0
    with _LOCK:
        return _read().get(email, 0)


def remaining(email: str | None) -> int | None:
    """Free reviews left, or None when the account is not rationed."""
    if email in EXEMPT:
        return None
    return max(0, FREE_REVIEWS - used(email))


def record(email: str | None) -> None:
    """Count one completed review against an account."""
    if email in EXEMPT:
        return
    with _LOCK:
        counts = _read()
        counts[email] = counts.get(email, 0) + 1
        PATH.parent.mkdir(parents=True, exist_ok=True)
        # Written beside and moved into place, so a restart in the middle leaves
        # the old count rather than a truncated file that reads as zero.
        temporary = PATH.with_suffix(".json.new")
        temporary.write_text(json.dumps(counts), encoding="utf-8")
        temporary.replace(PATH)

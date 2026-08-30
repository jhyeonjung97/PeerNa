"""Where finished reviews are recorded.

Shared by the CLI and the web server so a run shows up in the history whichever
way it was started — the two are the same tool, and a report that exists only in
a terminal scrollback is one the user cannot find again.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

JOB_DIR = Path.home() / ".cache" / "ai-peer-reviewer" / "jobs"
KEEP = 50


def save(record: dict) -> str:
    """Write one finished review. Returns its id."""
    job_id = record.get("job_id") or uuid.uuid4().hex
    record = {**record, "job_id": job_id}
    try:
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        (JOB_DIR / f"{job_id}.json").write_text(json.dumps(record), encoding="utf-8")
        stale = sorted(JOB_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in stale[:-KEEP]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # never let a cache write failure lose the review itself
    return job_id


def record_cli_run(filename, html, markdown, cost, duration, warnings, tokens, web_search):
    return save({
        "status": "done",
        "filename": filename,
        "html": html,
        "markdown": markdown,
        "cost": round(cost, 4),
        "duration": int(duration),
        "warnings": list(dict.fromkeys(warnings)),
        "tokens": tokens,
        "web_search": web_search,
        "started_at": time.time() - duration,
        "step": 5,
        "total": 5,
        "stage": "Complete",
        "source": "cli",
    })

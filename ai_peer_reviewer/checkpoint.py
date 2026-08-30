"""Persist completed passes so a failed run can be resumed.

A review is four expensive calls followed by a consolidation. Losing all of them
because the fourth hit a rate limit means paying twice for the same work, so
each pass is written to disk the moment it returns. Re-running the same command
picks up where it stopped.

A run is identified by the manuscript's contents, the mode, and the model, so
editing the manuscript or switching model starts a fresh run rather than
silently reusing notes about a different document.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

CACHE_ROOT = Path.home() / ".cache" / "ai-peer-reviewer" / "runs"

#: Runs older than this are cleared on the next start.
MAX_AGE_DAYS = 14


def run_id(manuscript_bytes: bytes, mode: str, model_id: str) -> str:
    digest = hashlib.sha256()
    digest.update(manuscript_bytes)
    digest.update(mode.encode())
    digest.update(model_id.encode())
    return digest.hexdigest()[:16]


class Checkpoint:
    def __init__(self, identifier: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self.dir = CACHE_ROOT / identifier
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
            _sweep_old_runs()

    def load(self, key: str) -> str | None:
        if not self.enabled:
            return None
        path = self.dir / f"{key}.txt"
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        return text if text.strip() else None

    def save(self, key: str, value: str) -> None:
        if not self.enabled or not value.strip():
            return
        (self.dir / f"{key}.txt").write_text(value, encoding="utf-8")
        self._touch()

    def record_usage(self, usage: dict) -> None:
        """Carry spend across resumptions so the reported total stays honest."""
        if not self.enabled:
            return
        path = self.dir / "usage.json"
        running = {}
        if path.is_file():
            try:
                running = json.loads(path.read_text())
            except Exception:
                running = {}
        for name, value in usage.items():
            running[name] = running.get(name, 0) + value
        path.write_text(json.dumps(running), encoding="utf-8")

    def prior_usage(self) -> dict:
        path = self.dir / "usage.json"
        if not self.enabled or not path.is_file():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _touch(self) -> None:
        (self.dir / ".stamp").write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )


def _sweep_old_runs() -> None:
    if not CACHE_ROOT.is_dir():
        return
    cutoff = datetime.now(timezone.utc).timestamp() - MAX_AGE_DAYS * 86400
    for entry in CACHE_ROOT.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass

"""Shared lock so only one program drives Chrome's debug port at a time.

Path: ``%USERPROFILE%\\InvestorHelper\\chrome.lock`` (override with the
``chrome_lock_path`` setting). Contents are JSON::

    {"owner": "investor-helper", "pid": 1234, "started_at": "2026-11-05T20:50:00+00:00"}

Create it (exclusively) before using port 9222, delete it afterwards. A lock
older than 15 minutes is stale and may be taken over.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

STALE_SECONDS = 15 * 60
OWNER = "investor-helper"


def default_path() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "InvestorHelper" / "chrome.lock"


class ChromeBusy(RuntimeError):
    pass


class ChromeLock:
    def __init__(self, path: Path | str | None = None, owner: str = OWNER):
        self.path = Path(path) if path else default_path()
        self.owner = owner
        self.held = False

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _age(self, info: dict) -> float:
        try:
            t = datetime.fromisoformat(info["started_at"])
            return (datetime.now(timezone.utc) - t).total_seconds()
        except (KeyError, ValueError, TypeError):
            try:
                return datetime.now().timestamp() - self.path.stat().st_mtime
            except OSError:
                return 0.0

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"owner": self.owner, "pid": os.getpid(),
                              "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                self.held = True
                return
            except FileExistsError:
                info = self._read()
                if self._age(info) > STALE_SECONDS:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                raise ChromeBusy(f"Chrome is in use by {info.get('owner', 'another program')} "
                                 f"since {info.get('started_at', '?')}")
        raise ChromeBusy("Could not take the Chrome lock")

    def release(self) -> None:
        if self.held and self._read().get("pid") == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.held = False

    def __enter__(self) -> "ChromeLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

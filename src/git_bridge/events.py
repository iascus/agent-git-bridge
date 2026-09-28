"""Structured event logging and an optional append-only JSON Lines audit log.

Events carry identifiers and outcomes only: never file contents, patch
contents, commit messages or credentials.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("git_bridge")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventLog:
    def __init__(self, audit_path: Path | None = None) -> None:
        self.audit_path = audit_path
        self._lock = threading.Lock()
        if audit_path is not None:
            audit_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: object) -> None:
        record = {"ts": utc_now(), "event": event, **fields}
        line = json.dumps(record, sort_keys=True, default=str)
        log.info(line)
        if self.audit_path is None:
            return
        with self._lock:
            # O_APPEND: records are only ever added, never rewritten.
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._ -]+")


class StorageError(RuntimeError):
    """Base storage error."""


class ContentNotFound(StorageError):
    """Raised when no current content has been imported."""


@dataclass(frozen=True)
class StoredImport:
    content_path: Path
    meta_path: Path
    metadata: dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(UTC)


def sanitize_filename(filename: str | None) -> str | None:
    if not filename:
        return None
    name = Path(filename).name.strip()
    if not name:
        return None
    name = FILENAME_SAFE_RE.sub("_", name)
    return name[:255] or None


class FileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def content_path(self) -> Path:
        return self.root / "content.txt"

    @property
    def meta_path(self) -> Path:
        return self.root / "meta.json"

    def save(
        self,
        content: str,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        created_at: datetime | None = None,
    ) -> StoredImport:
        self.ensure_root()
        created = created_at or utc_now()
        encoded = content.encode("utf-8")
        metadata = {
            "filename": sanitize_filename(filename),
            "content_type": content_type or "text/plain",
            "created_at": format_timestamp(created),
            "size_bytes": len(encoded),
        }

        content_tmp = self.root / "content.txt.tmp"
        meta_tmp = self.root / "meta.json.tmp"
        content_tmp.write_text(content, encoding="utf-8")
        meta_tmp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        content_tmp.replace(self.content_path)
        meta_tmp.replace(self.meta_path)
        return StoredImport(self.content_path, self.meta_path, metadata)

    def get_content(self) -> str:
        if not self.content_path.is_file():
            raise ContentNotFound("No content has been imported")
        return self.content_path.read_text(encoding="utf-8")

    def get_metadata(self) -> dict[str, Any]:
        if not self.meta_path.is_file():
            raise ContentNotFound("No content has been imported")
        return json.loads(self.meta_path.read_text(encoding="utf-8"))


def cleanup_expired_imports(storage_dir: Path, retention_hours: int, *, now: datetime | None = None) -> int:
    if retention_hours < 0:
        raise ValueError("retention_hours must be non-negative")
    if not storage_dir.exists():
        return 0

    cutoff = (now or utc_now()) - timedelta(hours=retention_hours)
    meta_path = storage_dir / "meta.json"
    content_path = storage_dir / "content.txt"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        created_at = parse_timestamp(metadata["created_at"])
    except (OSError, KeyError, json.JSONDecodeError, ValueError):
        return 0

    if created_at >= cutoff:
        return 0

    removed = 0
    for path in (content_path, meta_path):
        try:
            path.unlink()
            removed = 1
        except FileNotFoundError:
            pass
    return removed

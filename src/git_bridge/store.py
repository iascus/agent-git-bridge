"""Snapshot transports: where exported files and ``snapshot.json`` end up.

A store is an outbound copy of one Git commit, never a source of truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import SnapshotError

SNAPSHOT_NAME = "snapshot.json"


@dataclass(frozen=True)
class FileMetadata:
    blob_sha: str
    size: int
    file_class: str
    mime_type: str


@dataclass(frozen=True)
class StoredFile:
    path: str
    file_id: str
    # Git blob SHA of the stored content, if known; used to skip unchanged files.
    blob_sha: str | None


def check_relative_path(path: str) -> PurePosixPath:
    """Repository-relative POSIX path with no traversal components."""
    pure = PurePosixPath(path)
    if (
        not path
        or pure.is_absolute()
        or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
        or path == SNAPSHOT_NAME
    ):
        raise SnapshotError(f"refusing to export unsafe path {path[:200]!r}")
    return pure


def git_blob_sha1(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


class SnapshotStore(ABC):
    """Outbound snapshot transport. Implementations must be idempotent."""

    @abstractmethod
    def existing_files(self) -> dict[str, StoredFile]:
        """Files previously exported by the bridge, keyed by repository path."""

    @abstractmethod
    def put_file(self, path: str, content: bytes, metadata: FileMetadata) -> StoredFile:
        """Create or update (in place) the file at ``path``."""

    @abstractmethod
    def delete_file(self, path: str) -> None:
        """Remove a previously exported file."""

    @abstractmethod
    def put_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Write ``snapshot.json`` at the export root."""

    @abstractmethod
    def get_snapshot(self) -> dict[str, Any] | None:
        """Current ``snapshot.json`` content, or None."""


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gb-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class LocalDirectorySnapshotStore(SnapshotStore):
    """Exports into a local directory. For development, tests, or a folder
    synchronised by another tool."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _target(self, path: str) -> Path:
        rel = check_relative_path(path)
        target = self.root.joinpath(*rel.parts)
        root = self.root.resolve()
        if not target.resolve().is_relative_to(root):
            raise SnapshotError(f"path escapes export root: {path[:200]!r}")
        return target

    def existing_files(self) -> dict[str, StoredFile]:
        found: dict[str, StoredFile] = {}
        if not self.root.exists():
            return found
        for file in self.root.rglob("*"):
            if not file.is_file() or file.is_symlink():
                continue
            rel = file.relative_to(self.root).as_posix()
            if rel == SNAPSHOT_NAME or file.name.startswith(".gb-"):
                continue
            found[rel] = StoredFile(rel, rel, git_blob_sha1(file.read_bytes()))
        return found

    def put_file(self, path: str, content: bytes, metadata: FileMetadata) -> StoredFile:
        _atomic_write(self._target(path), content)
        return StoredFile(path, path, metadata.blob_sha)

    def delete_file(self, path: str) -> None:
        target = self._target(path)
        target.unlink(missing_ok=True)
        parent = target.parent
        root = self.root.resolve()
        while parent.resolve() != root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    def put_snapshot(self, snapshot: dict[str, Any]) -> None:
        data = json.dumps(snapshot, indent=2, sort_keys=False).encode("utf-8") + b"\n"
        _atomic_write(self.root / SNAPSHOT_NAME, data)

    def get_snapshot(self) -> dict[str, Any] | None:
        try:
            return json.loads((self.root / SNAPSHOT_NAME).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

"""Snapshot transports: where a refresh generation's artifacts end up.

A generation consists of two artifacts with stable names:

- ``snapshot``      the complete integration-branch ZIP (``<key>-snapshot.zip``)
- ``working_diff``  the integration -> working tree overlay (``<key>-working.diff``)

A store keeps each role's current artifact plus a small metadata record
(generation ID, commits, SHA-256, fingerprint). It is an outbound copy,
never a source of truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Literal

from .errors import SnapshotError

SNAPSHOT_NAME = "snapshot.json"  # manifest inside the snapshot ZIP
Role = Literal["snapshot", "working_diff"]
ROLES: tuple[Role, ...] = ("snapshot", "working_diff")


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
    """Outbound transport for a generation's artifacts."""

    @abstractmethod
    def put_artifact(self, role: Role, name: str, data: bytes, mime_type: str, info: dict[str, str]) -> str:
        """Create or replace in place (same identity) the artifact of ``role``."""

    @abstractmethod
    def get_artifact_info(self, role: Role) -> dict[str, str] | None:
        """Metadata recorded with the current artifact of ``role`` (incl. ``name``)."""


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
    """Writes artifacts into a local directory (development, tests, or a
    folder synchronised by another tool).

    ``branch`` is ``None`` for a repository's default pair (unchanged
    sidecar name, for continuity with stores created before this existed),
    or an additional working branch's name, which only changes the sidecar
    metadata filename: artifact file names are already branch-specific
    (``<key>-<branch>-snapshot.zip``), so only the role's own bookkeeping
    file needs to stay out of the default pair's way."""

    def __init__(self, root: Path, *, branch: str | None = None) -> None:
        self.root = Path(root)
        self.branch = branch

    def _info_path(self, role: Role) -> Path:
        suffix = role if self.branch is None else f"{role}@{self.branch}"
        return self.root / f".gb-{suffix}.json"

    def put_artifact(self, role: Role, name: str, data: bytes, mime_type: str, info: dict[str, str]) -> str:
        if "/" in name or "\\" in name or name.startswith("."):
            raise SnapshotError(f"invalid artifact name {name!r}")
        previous = self.get_artifact_info(role)
        _atomic_write(self.root / name, data)
        _atomic_write(self._info_path(role), json.dumps({**info, "name": name}).encode("utf-8"))
        if previous and previous.get("name") not in (None, name):
            (self.root / previous["name"]).unlink(missing_ok=True)
        return name

    def get_artifact_info(self, role: Role) -> dict[str, str] | None:
        try:
            return json.loads(self._info_path(role).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

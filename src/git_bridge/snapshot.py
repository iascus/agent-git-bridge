"""Snapshot generation (from Git objects of exactly one commit) and
consistent export to a SnapshotStore.

Export protocol:
  1. write snapshot.json with state "updating" (readers must not trust files)
  2. upload changed files in place, remove files no longer exported
  3. write the final snapshot.json with state "complete"  -- always last
"""

from __future__ import annotations

import mimetypes
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import pathglob
from .config import ExportConfig
from .errors import SnapshotError
from .events import utc_now
from .gitcmd import Git
from .store import SNAPSHOT_NAME, FileMetadata, SnapshotStore, check_relative_path

FORMAT_VERSION = 1

READER_NOTES = (
    "Exact export of one Git commit. If state is not 'complete', files may be "
    "mid-update: wait and re-read snapshot.json. Read bootstrap and index files "
    "at conversation start; read lazy files only when needed. A file whose "
    "'exported' is false is listed for completeness but not present in Drive. "
    "To propose changes, build publish.zip against 'commit' as expected_base_sha."
)

_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".toml": "text/plain",
    ".csv": "text/csv",
}


def mime_type_for(path: str) -> str:
    suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if suffix in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[suffix]
    guessed, _ = mimetypes.guess_type(path)
    return guessed if guessed and guessed.startswith("text/") else "text/plain"


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str
    blob_sha: str
    size: int


def list_tree(git: Git, commit: str) -> list[TreeEntry]:
    """All blobs of ``commit`` (recursive). Submodules (gitlinks) are skipped."""
    out = git.run(["ls-tree", "-r", "-z", "--long", "--full-tree", commit]).stdout
    entries = []
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode, obj_type, sha, size = meta.decode("ascii").split()
        if obj_type != "blob":
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        entries.append(TreeEntry(path=path, mode=mode, blob_sha=sha, size=int(size)))
    return entries


def read_blobs(git: Git, shas: list[str]) -> dict[str, bytes]:
    """Read many blobs through one ``git cat-file --batch`` process."""
    if not shas:
        return {}
    unique = list(dict.fromkeys(shas))
    out = git.run(["cat-file", "--batch"], input=("\n".join(unique) + "\n").encode("ascii")).stdout
    blobs: dict[str, bytes] = {}
    pos = 0
    for sha in unique:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode("ascii").split()
        if len(header) != 3 or header[0] != sha or header[1] != "blob":
            raise SnapshotError(f"unexpected cat-file output for {sha}")
        size = int(header[2])
        start = nl + 1
        blobs[sha] = out[start : start + size]
        pos = start + size + 1  # trailing newline
    return blobs


def classify(path: str, export: ExportConfig) -> str | None:
    """bootstrap | index | lazy, or None when the path is not exported."""
    if pathglob.match_any(path, export.exclude):
        return None
    if pathglob.match_any(path, export.bootstrap):
        return "bootstrap"
    if pathglob.match_any(path, export.indexes):
        return "index"
    if pathglob.match_any(path, export.lazy):
        return "lazy"
    return None


def is_text(content: bytes) -> bool:
    if b"\0" in content[:8000]:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


@dataclass
class SnapshotBuild:
    manifest: dict[str, Any]
    # path -> content, only for files that will be exported
    contents: dict[str, bytes] = field(default_factory=dict)


def new_generation_id(commit: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{commit[:12]}-{secrets.token_hex(3)}"


def build_snapshot(
    git: Git,
    *,
    commit: str,
    repository: str,
    repository_key: str,
    branch: str,
    export: ExportConfig,
) -> SnapshotBuild:
    tree = git.text(["rev-parse", "--verify", "--end-of-options", f"{commit}^{{tree}}"])
    selected: list[tuple[TreeEntry, str]] = []
    for entry in list_tree(git, commit):
        file_class = classify(entry.path, export)
        if file_class is not None:
            selected.append((entry, file_class))

    files: dict[str, dict[str, Any]] = {}
    to_read: list[TreeEntry] = []
    for entry, file_class in selected:
        info: dict[str, Any] = {
            "blob_sha": entry.blob_sha,
            "size": entry.size,
            "class": file_class,
            "exported": False,
        }
        files[entry.path] = info
        try:
            check_relative_path(entry.path)
            entry.path.encode("utf-8")
        except (SnapshotError, UnicodeEncodeError):
            info["reason"] = "unsupported_path"
            continue
        if entry.mode == "120000":
            info["reason"] = "symlink"
        elif entry.size > export.max_file_bytes:
            info["reason"] = "too_large"
        else:
            to_read.append(entry)

    total = sum(e.size for e in to_read)
    if total > export.max_total_bytes:
        raise SnapshotError(f"export would be {total} bytes, above max_total_bytes {export.max_total_bytes}")

    blobs = read_blobs(git, [e.blob_sha for e in to_read])
    contents: dict[str, bytes] = {}
    for entry in to_read:
        data = blobs[entry.blob_sha]
        info = files[entry.path]
        if not is_text(data):
            info["reason"] = "binary"
            continue
        info["exported"] = True
        info["mime_type"] = mime_type_for(entry.path)
        contents[entry.path] = data

    manifest = {
        "format_version": FORMAT_VERSION,
        "state": "complete",
        "generation_id": new_generation_id(commit),
        "repository": repository,
        "repository_key": repository_key,
        "branch": branch,
        "commit": commit,
        "tree": tree,
        "generated_at": None,  # set at publication time
        "previous_commit": None,
        "drive_root": export.drive_root,
        "snapshot_file": SNAPSHOT_NAME,
        "reader_notes": READER_NOTES,
        "counts": {
            "listed": len(files),
            "exported": len(contents),
            "bootstrap": sum(1 for f in files.values() if f["class"] == "bootstrap" and f["exported"]),
            "index": sum(1 for f in files.values() if f["class"] == "index" and f["exported"]),
            "lazy": sum(1 for f in files.values() if f["class"] == "lazy" and f["exported"]),
        },
        "files": dict(sorted(files.items())),
    }
    return SnapshotBuild(manifest=manifest, contents=contents)


@dataclass
class ExportResult:
    manifest: dict[str, Any]
    uploaded: int = 0
    unchanged: int = 0
    deleted: int = 0


def _previous_commit(store: SnapshotStore) -> str | None:
    previous = store.get_snapshot()
    if not previous:
        return None
    if previous.get("state") == "complete":
        return previous.get("commit")
    return previous.get("previous_commit")


def export_snapshot(store: SnapshotStore, build: SnapshotBuild) -> ExportResult:
    manifest = dict(build.manifest)
    try:
        previous_commit = _previous_commit(store)
        manifest["previous_commit"] = previous_commit
        store.put_snapshot(
            {
                "format_version": FORMAT_VERSION,
                "state": "updating",
                "generation_id": manifest["generation_id"],
                "repository": manifest["repository"],
                "repository_key": manifest["repository_key"],
                "branch": manifest["branch"],
                "previous_commit": previous_commit,
                "target_commit": manifest["commit"],
                "started_at": utc_now(),
                "reader_notes": READER_NOTES,
            }
        )

        result = ExportResult(manifest=manifest)
        existing = store.existing_files()
        files = {path: dict(info) for path, info in manifest["files"].items()}
        for path, content in build.contents.items():
            info = files[path]
            current = existing.get(path)
            if current is not None and current.blob_sha == info["blob_sha"]:
                info["drive_file_id"] = current.file_id
                result.unchanged += 1
                continue
            stored = store.put_file(
                path,
                content,
                FileMetadata(
                    blob_sha=info["blob_sha"], size=info["size"], file_class=info["class"], mime_type=info["mime_type"]
                ),
            )
            info["drive_file_id"] = stored.file_id
            result.uploaded += 1
        for path in sorted(set(existing) - set(build.contents)):
            store.delete_file(path)
            result.deleted += 1

        manifest["files"] = files
        manifest["generated_at"] = utc_now()
        store.put_snapshot(manifest)  # last: only now does the snapshot claim the new commit
        return result
    except SnapshotError:
        raise
    except Exception as exc:  # transport failures (HTTP, auth, disk)
        raise SnapshotError(f"snapshot export failed: {type(exc).__name__}: {str(exc)[:500]}") from exc

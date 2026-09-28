"""Snapshot generation (from Git objects of exactly one commit) and
consistent export to a SnapshotStore.

Archive format (default): one deterministic ZIP per repository containing
snapshot.json and the selected files, replaced in place in a single write.

Files format:
  1. write snapshot.json with state "updating" (readers must not trust files)
  2. upload changed files in place, remove files no longer exported
  3. write the final snapshot.json with state "complete"  -- always last
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import secrets
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import pathglob
from .config import ExportConfig
from .manifest import parse_manifest
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
    entries = list_tree(git, commit)
    selection_info: dict[str, Any]
    # (entry, class, lazy class name)
    selected: list[tuple[TreeEntry, str, str | None]] = []
    missing: list[tuple[str, str, str | None]] = []
    if export.manifest is not None:
        by_path = {e.path: e for e in entries}
        manifest_entry = by_path.get(export.manifest)
        if manifest_entry is None:
            raise SnapshotError(f"manifest {export.manifest} does not exist at commit {commit[:12]}")
        selection = parse_manifest(read_blobs(git, [manifest_entry.blob_sha])[manifest_entry.blob_sha], export.manifest)
        for path in selection.files:
            if pathglob.match_any(path, export.exclude):
                continue
            file_class, lazy_class = selection.classify(path)
            if path in by_path:
                selected.append((by_path[path], file_class, lazy_class))
            else:
                missing.append((path, file_class, lazy_class))
        selection_info = {"source": "manifest", "manifest": export.manifest, "manifest_blob_sha": manifest_entry.blob_sha}
    else:
        for entry in entries:
            file_class = classify(entry.path, export)
            if file_class is not None:
                selected.append((entry, file_class, None))
        selection_info = {"source": "rules"}

    files: dict[str, dict[str, Any]] = {}
    for path, file_class, lazy_class in missing:
        files[path] = {"blob_sha": None, "size": 0, "class": file_class, "exported": False, "reason": "missing"}
        if lazy_class:
            files[path]["lazy_class"] = lazy_class
    to_read: list[TreeEntry] = []
    for entry, file_class, lazy_class in selected:
        info: dict[str, Any] = {
            "blob_sha": entry.blob_sha,
            "size": entry.size,
            "class": file_class,
            "exported": False,
        }
        if lazy_class:
            info["lazy_class"] = lazy_class
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
        "selection": selection_info,
        "snapshot_file": SNAPSHOT_NAME,
        "reader_notes": READER_NOTES,
        "counts": {
            "listed": len(files),
            "exported": len(contents),
            "bootstrap": sum(1 for f in files.values() if f["class"] == "bootstrap" and f["exported"]),
            "index": sum(1 for f in files.values() if f["class"] == "index" and f["exported"]),
            "lazy": sum(1 for f in files.values() if f["class"] == "lazy" and f["exported"]),
            "missing": len(missing),
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
    archive_name: str | None = None
    archive_bytes: int | None = None
    archive_sha256: str | None = None


ARCHIVE_READER_NOTES = (
    "Exact export of one Git commit as a single ZIP, replaced atomically. "
    "Unzip it; this snapshot.json describes every file in the archive. Read "
    "bootstrap files at conversation start and lazy files only when needed. "
    "To propose changes, build publish.zip against 'commit' as expected_base_sha."
)
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def archive_fingerprint(manifest: dict[str, Any]) -> str:
    """Identifies archive content independent of generation time."""
    material = {
        "commit": manifest["commit"],
        "selection": manifest.get("selection"),
        "files": {
            p: [f["blob_sha"], f["class"], f.get("lazy_class"), f["exported"], f.get("reason")]
            for p, f in manifest["files"].items()
        },
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def build_archive(manifest: dict[str, Any], contents: dict[str, bytes]) -> bytes:
    """Deterministic ZIP: fixed timestamps and permissions, sorted entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:

        def add(name: str, data: bytes) -> None:
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)

        add(SNAPSHOT_NAME, json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
        for path in sorted(contents):
            add(path, contents[path])
    return buf.getvalue()


def export_archive(store: SnapshotStore, build: SnapshotBuild, name: str) -> ExportResult:
    manifest = dict(build.manifest)
    manifest["reader_notes"] = ARCHIVE_READER_NOTES
    manifest.pop("snapshot_file", None)
    manifest["archive"] = name
    try:
        info = store.get_archive_info() or {}
        manifest["previous_commit"] = info.get("commit")
        fingerprint = archive_fingerprint(manifest)
        result = ExportResult(manifest=manifest, archive_name=name)
        if info.get("fingerprint") == fingerprint and info.get("name") == name:
            result.unchanged = 1
            manifest["generation_id"] = info.get("generation_id", manifest["generation_id"])
            manifest["generated_at"] = info.get("generated_at")
            result.archive_sha256 = info.get("sha256")
            result.archive_bytes = int(info["bytes"]) if info.get("bytes", "").isdigit() else None
        else:
            manifest["generated_at"] = utc_now()
            data = build_archive(manifest, build.contents)
            digest = hashlib.sha256(data).hexdigest()
            store.put_archive(
                name,
                data,
                {
                    "commit": manifest["commit"],
                    "fingerprint": fingerprint,
                    "generation_id": manifest["generation_id"],
                    "generated_at": manifest["generated_at"],
                    "sha256": digest,
                    "bytes": str(len(data)),
                    "files": str(len(build.contents)),
                    "branch": manifest["branch"],
                },
            )
            result.uploaded = 1
            result.archive_sha256 = digest
            result.archive_bytes = len(data)
        # Only after the archive is in place: drop artefacts of the files format.
        result.deleted = store.remove_file_exports()
        return result
    except SnapshotError:
        raise
    except Exception as exc:  # transport failures (HTTP, auth, disk)
        raise SnapshotError(f"snapshot export failed: {type(exc).__name__}: {str(exc)[:500]}") from exc


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

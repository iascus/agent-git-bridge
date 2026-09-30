"""Refresh generations: integration snapshot + working-branch overlay.

One generation is built from one resolved pair of commits:

    M = integration branch head   (e.g. main)
    D = working branch head       (e.g. design-docs)

and produces two artifacts:

    <key>-snapshot.zip    snapshot.json + the manifest-selected files of M
    <key>-working.diff    the tree diff  sel(M) -> sel(D)

where sel(X) is the tree of X restricted to the files X's own manifest
lists (text files within the size limits). The overlay is computed between
two synthetic trees built from exactly those files, so

    unzip(snapshot) + git apply working.diff  ==  sel(D), byte for byte

regardless of how the branches' histories relate (behind, ahead, divergent,
rebased). The bridge never merges or rebases anything.

Export order: the overlay is written first and the snapshot last; the
snapshot's snapshot.json pins the overlay's SHA-256 and generation ID, so a
reader can always detect a mismatched pair.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import ExportConfig
from .errors import GitError, SnapshotError
from .events import utc_now
from .gitcmd import Git
from .manifest import parse_manifest
from .store import SNAPSHOT_NAME, SnapshotStore, check_relative_path

FORMAT_VERSION = 2
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

READER_NOTES = (
    "Two Git states of one project. The files in this ZIP are the integration "
    "branch at integration_commit (M). The working-branch overlay (working_diff) "
    "is the exact tree diff from these files to the working branch at "
    "working_commit (D): apply it with `git apply` to reconstruct D; if "
    "working_diff.empty is true, D's files equal M's. First verify the overlay's "
    "sha256 (and its '# generation:' header) against working_diff; on mismatch a "
    "refresh is in progress, so fetch both files again. Pushes are made against "
    "D: expected_base_sha = working_commit, changes.patch = D -> your HEAD."
)


# --------------------------------------------------------------- git reading


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str
    blob_sha: str
    size: int


def list_tree(git: Git, commit: str) -> dict[str, TreeEntry]:
    """All blobs of ``commit`` (recursive). Submodules are skipped."""
    out = git.run(["ls-tree", "-r", "-z", "--long", "--full-tree", commit]).stdout
    entries: dict[str, TreeEntry] = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode, obj_type, sha, size = meta.decode("ascii").split()
        if obj_type != "blob":
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        entries[path] = TreeEntry(path=path, mode=mode, blob_sha=sha, size=int(size))
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
        pos = start + size + 1
    return blobs


def is_text(content: bytes) -> bool:
    if b"\0" in content[:8000]:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


# ------------------------------------------------------------------ selection


@dataclass
class Selection:
    """The exported files of one commit, as selected by its own manifest."""

    commit: str
    manifest_blob_sha: str
    files: dict[str, TreeEntry] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    not_exported: dict[str, str] = field(default_factory=dict)  # path -> reason


def select_files(git: Git, commit: str, export: ExportConfig) -> Selection:
    tree = list_tree(git, commit)
    manifest_entry = tree.get(export.manifest)
    if manifest_entry is None:
        raise SnapshotError(f"manifest {export.manifest} does not exist at commit {commit[:12]}")
    listed = parse_manifest(read_blobs(git, [manifest_entry.blob_sha])[manifest_entry.blob_sha], export.manifest)
    selection = Selection(commit=commit, manifest_blob_sha=manifest_entry.blob_sha)
    candidates: list[TreeEntry] = []
    for path in listed:
        entry = tree.get(path)
        try:
            check_relative_path(path)  # also reserves snapshot.json for the manifest
        except SnapshotError:
            selection.not_exported[path] = "unsupported_path"
            continue
        if entry is None:
            selection.not_exported[path] = "missing"
        elif entry.mode == "120000":
            selection.not_exported[path] = "symlink"
        elif entry.size > export.max_file_bytes:
            selection.not_exported[path] = "too_large"
        else:
            candidates.append(entry)
    total = sum(e.size for e in candidates)
    if total > export.max_total_bytes:
        raise SnapshotError(f"export would be {total} bytes, above max_total_bytes {export.max_total_bytes}")
    blobs = read_blobs(git, [e.blob_sha for e in candidates])
    for entry in candidates:
        data = blobs[entry.blob_sha]
        if not is_text(data):
            selection.not_exported[entry.path] = "binary"
            continue
        selection.files[entry.path] = entry
        selection.contents[entry.path] = data
    return selection


# ------------------------------------------------------------------- overlay


def synthetic_tree(git: Git, entries: dict[str, TreeEntry]) -> str:
    """Write a tree object containing exactly ``entries`` (via a throwaway index)."""
    fd, index_path = tempfile.mkstemp(prefix="gb-index-")
    os.close(fd)
    os.unlink(index_path)  # git creates it
    env = {"GIT_INDEX_FILE": index_path}
    try:
        if entries:
            lines = b"".join(
                f"{e.mode} {e.blob_sha}\t".encode("ascii") + e.path.encode("utf-8") + b"\0"
                for e in sorted(entries.values(), key=lambda e: e.path)
            )
            git.run(["update-index", "-z", "--index-info"], input=lines, env=env)
        return git.text(["write-tree"], env=env)
    finally:
        for suffix in ("", ".lock"):
            try:
                os.unlink(index_path + suffix)
            except FileNotFoundError:
                pass


_DIFF_ARGS = ["-M", "--full-index", "--no-color", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/"]


def tree_diff(git: Git, from_tree: str, to_tree: str) -> bytes:
    """Deterministic text patch transforming ``from_tree`` into ``to_tree``."""
    return git.run(["diff-tree", "-p", *_DIFF_ARGS, from_tree, to_tree]).stdout


def tree_diff_stats(git: Git, from_tree: str, to_tree: str) -> tuple[int, int, int]:
    out = git.text(["diff-tree", "--shortstat", "-M", from_tree, to_tree])
    numbers = {k: int(v) for v, k in re.findall(r"(\d+) (file|insertion|deletion)", out)}
    return numbers.get("file", 0), numbers.get("insertion", 0), numbers.get("deletion", 0)


# ----------------------------------------------------------------- generation


@dataclass
class Generation:
    generation_id: str
    fingerprint: str
    manifest: dict[str, Any]  # snapshot.json
    snapshot_name: str
    snapshot_zip: bytes
    diff_name: str
    diff_bytes: bytes


def new_generation_id(integration: str, working: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{integration[:8]}-{working[:8]}-{secrets.token_hex(3)}"


def _branch_relationship(git: Git, integration: str, working: str) -> tuple[str | None, int | None, int | None]:
    base = git.run(["merge-base", integration, working], check=False)
    merge_base = base.text if base.returncode == 0 and base.text else None
    try:
        behind, ahead = git.text(["rev-list", "--left-right", "--count", f"{integration}...{working}"]).split()
        return merge_base, int(ahead), int(behind)
    except (GitError, ValueError):
        return merge_base, None, None


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


def build_generation(
    git: Git,
    *,
    integration_commit: str,
    working_commit: str,
    repository: str,
    project_key: str,
    integration_branch: str,
    working_branch: str,
    export: ExportConfig,
    snapshot_name: str,
    diff_name: str,
) -> Generation:
    base = select_files(git, integration_commit, export)
    work = select_files(git, working_commit, export)
    base_tree = synthetic_tree(git, base.files)
    work_tree = synthetic_tree(git, work.files)
    body = tree_diff(git, base_tree, work_tree)
    files_changed, insertions, deletions = tree_diff_stats(git, base_tree, work_tree)
    merge_base, ahead, behind = _branch_relationship(git, integration_commit, working_commit)

    fingerprint_material = {
        "format_version": FORMAT_VERSION,
        "integration": [integration_branch, integration_commit, base_tree, base.not_exported],
        "working": [working_branch, working_commit, work_tree, work.not_exported],
        "names": [snapshot_name, diff_name],
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_material, sort_keys=True).encode()).hexdigest()
    generation_id = new_generation_id(integration_commit, working_commit)

    header = (
        "# agent-git-bridge working-branch overlay\n"
        f"# generation: {generation_id}\n"
        f"# repository: {repository}\n"
        f"# base: {integration_branch} {integration_commit}\n"
        f"# target: {working_branch} {working_commit}\n"
        f"# {'empty: the working tree equals the integration tree' if not body else 'apply to the unzipped snapshot with: git apply'}\n"
        "\n"
    ).encode("utf-8")
    diff_bytes = header + body

    manifest: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "generation_id": generation_id,
        "generated_at": utc_now(),
        "repository": repository,
        "project_key": project_key,
        "integration_branch": integration_branch,
        "integration_commit": integration_commit,
        "working_branch": working_branch,
        "working_commit": working_commit,
        "merge_base_commit": merge_base,
        "working_ahead_by": ahead,
        "working_behind_by": behind,
        "manifest_path": export.manifest,
        "files": {p: {"blob_sha": e.blob_sha, "size": e.size} for p, e in sorted(base.files.items())},
        "not_exported": dict(sorted(base.not_exported.items())),
        "working_diff": {
            "filename": diff_name,
            "sha256": hashlib.sha256(diff_bytes).hexdigest(),
            "bytes": len(diff_bytes),
            "base_commit": integration_commit,
            "target_commit": working_commit,
            "empty": not body,
            "files_changed": files_changed,
            "insertions": insertions,
            "deletions": deletions,
            # What the working tree must look like after applying the overlay.
            "working_files": {p: e.blob_sha for p, e in sorted(work.files.items())},
            "working_not_exported": dict(sorted(work.not_exported.items())),
        },
        "reader_notes": READER_NOTES,
    }
    return Generation(
        generation_id=generation_id,
        fingerprint=fingerprint,
        manifest=manifest,
        snapshot_name=snapshot_name,
        snapshot_zip=build_archive(manifest, base.contents),
        diff_name=diff_name,
        diff_bytes=diff_bytes,
    )


# --------------------------------------------------------------------- export


@dataclass
class ExportResult:
    generation_id: str
    uploaded: bool
    snapshot_sha256: str
    snapshot_bytes: int


def export_generation(store: SnapshotStore, gen: Generation) -> ExportResult:
    """Write the overlay, then the snapshot (last). Skip if unchanged."""
    try:
        snap_info = store.get_artifact_info("snapshot") or {}
        diff_info = store.get_artifact_info("working_diff") or {}
        if (
            snap_info.get("fingerprint") == gen.fingerprint
            and diff_info.get("fingerprint") == gen.fingerprint
            and snap_info.get("generation_id") == diff_info.get("generation_id")
            and snap_info.get("name") == gen.snapshot_name
            and diff_info.get("name") == gen.diff_name
        ):
            return ExportResult(
                generation_id=snap_info["generation_id"],
                uploaded=False,
                snapshot_sha256=snap_info.get("sha256", ""),
                snapshot_bytes=int(snap_info.get("bytes", 0)),
            )
        m = gen.manifest
        common = {
            "generation_id": gen.generation_id,
            "fingerprint": gen.fingerprint,
            "integration_commit": m["integration_commit"],
            "working_commit": m["working_commit"],
            "generated_at": m["generated_at"],
        }
        store.put_artifact(
            "working_diff",
            gen.diff_name,
            gen.diff_bytes,
            "text/plain",
            {**common, "sha256": m["working_diff"]["sha256"], "bytes": str(len(gen.diff_bytes))},
        )
        snapshot_sha = hashlib.sha256(gen.snapshot_zip).hexdigest()
        store.put_artifact(
            "snapshot",
            gen.snapshot_name,
            gen.snapshot_zip,
            "application/zip",
            {**common, "sha256": snapshot_sha, "bytes": str(len(gen.snapshot_zip)), "files": str(len(m["files"]))},
        )
        return ExportResult(gen.generation_id, True, snapshot_sha, len(gen.snapshot_zip))
    except SnapshotError:
        raise
    except Exception as exc:  # transport failures (HTTP, auth, disk)
        raise SnapshotError(f"snapshot export failed: {type(exc).__name__}: {str(exc)[:500]}") from exc

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
from .manifest import parse_artifact_roots, parse_manifest
from .store import SNAPSHOT_NAME, SnapshotStore, check_relative_path

FORMAT_VERSION = 2
# Format 3: at least one branch's manifest declares PROJECT_ARTIFACT_ROOTS.
# Files under those roots are exported byte-for-byte whether text or binary,
# and the overlay may contain Git binary patch hunks. The diff command and
# ZIP layout are otherwise unchanged, and format 2 output is byte-identical
# to before this existed (--binary is a no-op on an all-text diff).
FORMAT_VERSION_ARTIFACTS = 3
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

READER_NOTES_ARTIFACTS = (
    READER_NOTES + " This project also declares project_artifact_roots: files under "
    "those roots (see artifact_roots) are included whether text or binary and are "
    "stored/patched byte-for-byte; `files[path].origin` and `working_diff."
    "working_artifact_files` mark which files came from an artifact root rather "
    "than the source manifest. The overlay may contain Git binary patch hunks "
    "(`git apply` reconstructs them the same way as text hunks)."
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
    """The exported files of one commit, as selected by its own manifest:
    sel(source) from PROJECT_SOURCE_FILES (text only) plus sel(artifacts)
    from every tracked file under a PROJECT_ARTIFACT_ROOTS root (text or
    binary)."""

    commit: str
    manifest_blob_sha: str
    files: dict[str, TreeEntry] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    not_exported: dict[str, str] = field(default_factory=dict)  # path -> reason
    artifact_roots: list[str] = field(default_factory=list)
    artifact_paths: set[str] = field(default_factory=set)  # subset of files.keys()


def _check_candidate(export: ExportConfig, entry: TreeEntry, not_exported: dict[str, str]) -> bool:
    try:
        check_relative_path(entry.path)
    except SnapshotError:
        not_exported[entry.path] = "unsupported_path"
        return False
    if entry.mode == "120000":
        not_exported[entry.path] = "symlink"
        return False
    if entry.size > export.max_file_bytes:
        not_exported[entry.path] = "too_large"
        return False
    return True


def select_files(git: Git, commit: str, export: ExportConfig) -> Selection:
    tree = list_tree(git, commit)
    manifest_entry = tree.get(export.manifest)
    if manifest_entry is None:
        raise SnapshotError(f"manifest {export.manifest} does not exist at commit {commit[:12]}")
    manifest_bytes = read_blobs(git, [manifest_entry.blob_sha])[manifest_entry.blob_sha]
    listed = parse_manifest(manifest_bytes, export.manifest)
    artifact_roots = parse_artifact_roots(manifest_bytes, export.manifest)
    selection = Selection(commit=commit, manifest_blob_sha=manifest_entry.blob_sha, artifact_roots=artifact_roots)

    def _under_root(path: str) -> bool:
        return any(path.startswith(root) for root in artifact_roots)

    source_candidates: list[TreeEntry] = []
    for path in listed:
        entry = tree.get(path)
        try:
            check_relative_path(path)  # also reserves snapshot.json for the manifest
        except SnapshotError:
            selection.not_exported[path] = "unsupported_path"
            continue
        if entry is None:
            selection.not_exported[path] = "missing"
        elif _under_root(path):
            continue  # under an artifact root: handled below, no text filter
        elif _check_candidate(export, entry, selection.not_exported):
            source_candidates.append(entry)

    # Every tracked file under a root, whether or not it is also individually
    # listed in PROJECT_SOURCE_FILES: the root rule always wins, so a binary
    # file does not need (and must not need) to be listed to be included.
    artifact_candidates: list[TreeEntry] = []
    if artifact_roots:
        for path, entry in tree.items():
            if not _under_root(path):
                continue
            if _check_candidate(export, entry, selection.not_exported):
                artifact_candidates.append(entry)

    total = sum(e.size for e in source_candidates) + sum(e.size for e in artifact_candidates)
    if total > export.max_total_bytes:
        raise SnapshotError(f"export would be {total} bytes, above max_total_bytes {export.max_total_bytes}")

    blobs = read_blobs(git, [e.blob_sha for e in (*source_candidates, *artifact_candidates)])
    for entry in source_candidates:
        data = blobs[entry.blob_sha]
        if not is_text(data):
            selection.not_exported[entry.path] = "binary"
            continue
        selection.files[entry.path] = entry
        selection.contents[entry.path] = data
    for entry in artifact_candidates:
        selection.files[entry.path] = entry
        selection.contents[entry.path] = blobs[entry.blob_sha]
        selection.artifact_paths.add(entry.path)
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


# --binary makes binary content appear as a literal/base85 GIT binary patch
# instead of "Binary files differ"; it is a byte-for-byte no-op on an
# all-text diff (verified: identical output with and without the flag), so
# this is safe for format-2 (text-only) projects too.
_DIFF_ARGS = [
    "-M",
    "--full-index",
    "--binary",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
]


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

    # Format 3 only when a branch's own manifest actually declares artifact
    # roots; otherwise the output is byte-identical to before this existed.
    has_artifacts = bool(base.artifact_roots or work.artifact_roots)
    format_version = FORMAT_VERSION_ARTIFACTS if has_artifacts else FORMAT_VERSION

    fingerprint_material = {
        "format_version": format_version,
        "integration": [integration_branch, integration_commit, base_tree, base.not_exported],
        "working": [working_branch, working_commit, work_tree, work.not_exported],
        "names": [snapshot_name, diff_name],
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_material, sort_keys=True).encode()).hexdigest()
    generation_id = new_generation_id(integration_commit, working_commit)

    header_lines = [
        "# agent-git-bridge working-branch overlay",
        f"# generation: {generation_id}",
        f"# repository: {repository}",
        f"# base: {integration_branch} {integration_commit}",
        f"# target: {working_branch} {working_commit}",
    ]
    if has_artifacts:
        roots = ", ".join(sorted(set(base.artifact_roots) | set(work.artifact_roots))) or "(none)"
        header_lines.append(f"# format: {format_version} (binary-capable; artifact roots: {roots})")
    header_lines.append(
        "# empty: the working tree equals the integration tree" if not body else "# apply to the unzipped snapshot with: git apply"
    )
    header = ("\n".join(header_lines) + "\n\n").encode("utf-8")
    diff_bytes = header + body

    files = {p: {"blob_sha": e.blob_sha, "size": e.size} for p, e in sorted(base.files.items())}
    working_diff: dict[str, Any] = {
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
    }
    if has_artifacts:
        for path, info in files.items():
            info["origin"] = "artifact_root" if path in base.artifact_paths else "source"
        working_diff["working_artifact_roots"] = sorted(work.artifact_roots)
        working_diff["working_artifact_files"] = sorted(work.artifact_paths)

    manifest: dict[str, Any] = {
        "format_version": format_version,
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
        "files": files,
        "not_exported": dict(sorted(base.not_exported.items())),
        "working_diff": working_diff,
        "reader_notes": READER_NOTES_ARTIFACTS if has_artifacts else READER_NOTES,
    }
    if has_artifacts:
        manifest["artifact_roots"] = sorted(base.artifact_roots)
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

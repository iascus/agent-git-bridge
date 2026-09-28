from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import BRANCH, GitEnv, git
from git_bridge.config import ExportConfig
from git_bridge.errors import SnapshotError
from git_bridge.snapshot import build_snapshot, classify, mime_type_for
from git_bridge.store import FileMetadata, LocalDirectorySnapshotStore, SnapshotStore, git_blob_sha1

EXPORT = ExportConfig(
    drive_root="ChatGPT/rot3k",
    bootstrap=["MANIFEST.md", "docs/**/*.md"],
    indexes=["characters/index*.json"],
    lazy=["characters/**/*.md", "records/**/*.md", "assets/**"],
    exclude=["records/private-*"],
)


class RecordingStore(SnapshotStore):
    """Wraps a real store and records the order of operations."""

    def __init__(self, inner: SnapshotStore, hooks: dict | None = None) -> None:
        self.inner = inner
        self.ops: list[tuple[str, str]] = []
        self.hooks = hooks or {}

    def existing_files(self):
        return self.inner.existing_files()

    def put_file(self, path, content, metadata):
        if "put_file" in self.hooks:
            self.hooks["put_file"](path)
        self.ops.append(("put_file", path))
        return self.inner.put_file(path, content, metadata)

    def delete_file(self, path):
        self.ops.append(("delete_file", path))
        self.inner.delete_file(path)

    def put_snapshot(self, snapshot):
        self.ops.append(("put_snapshot", snapshot["state"]))
        self.inner.put_snapshot(snapshot)

    def get_snapshot(self):
        return self.inner.get_snapshot()


class FailingStore(RecordingStore):
    def put_file(self, path, content, metadata):
        raise ConnectionError("Drive unavailable")


def _seed_repo(env: GitEnv) -> str:
    """Add a representative tree to the remote and return the new head."""
    env._reset_seed()
    env._write(
        {
            "docs/rules.md": "Rules\n",
            "characters/index.json": '{"a": 1}\n',
            "characters/alice.md": "Alice\n",
            "records/juan-23.md": "Juan\n",
            "records/private-notes.md": "secret\n",
            "src/unmatched.py": "print()\n",
            "assets/image.bin": bytes(range(256)),
            "snapshot.json": "{}\n",  # collides with the manifest name
        }
    )
    git(env.seed, "add", "-A")
    git(env.seed, "commit", "--quiet", "-m", "Seed tree")
    git(env.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
    return env.head()


@pytest.fixture
def snapenv(gitenv: GitEnv) -> GitEnv:
    gitenv.with_repo_config(export=EXPORT)
    _seed_repo(gitenv)
    export_root = gitenv.tmp / "export"
    gitenv.export_root = export_root
    gitenv.store_factory = lambda repo: LocalDirectorySnapshotStore(export_root)
    return gitenv


def _exported(env: GitEnv) -> dict[str, bytes]:
    root: Path = env.export_root
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.name != "snapshot.json"
    }


def _manifest(env: GitEnv) -> dict:
    return json.loads((env.export_root / "snapshot.json").read_text())


# ------------------------------------------------------------ classification


@pytest.mark.parametrize(
    "path,expected",
    [
        ("MANIFEST.md", "bootstrap"),
        ("docs/a/b.md", "bootstrap"),
        ("characters/index.json", "index"),
        ("characters/alice.md", "lazy"),
        ("records/private-x.md", None),
        ("src/app.py", None),
    ],
)
def test_classify(path, expected):
    assert classify(path, EXPORT) == expected


def test_mime_types():
    assert mime_type_for("a/b.md") == "text/markdown"
    assert mime_type_for("x.json") == "application/json"
    assert mime_type_for("Makefile") == "text/plain"


# ----------------------------------------------------------------- building


def test_snapshot_lists_exact_commit_with_correct_blob_shas(snapenv: GitEnv):
    head = snapenv.head()
    repo = snapenv.repo
    repo.ensure_clone()
    repo.fetch(BRANCH)
    build = build_snapshot(
        repo.git, commit=head, repository="iascus/rot3k", repository_key="rot3k", branch=BRANCH, export=EXPORT
    )
    m = build.manifest
    assert m["commit"] == head and m["format_version"] == 1
    assert m["tree"] == git(snapenv.origin, "rev-parse", f"{head}^{{tree}}")
    assert set(m["files"]) == {
        "MANIFEST.md", "docs/guide.md", "docs/rules.md", "characters/index.json",
        "characters/alice.md", "records/juan-23.md", "assets/image.bin",
    }
    for path, info in m["files"].items():
        assert info["blob_sha"] == git(snapenv.origin, "rev-parse", f"{head}:{path}")
        assert info["size"] == int(git(snapenv.origin, "cat-file", "-s", info["blob_sha"]))
        if info["exported"]:
            content = build.contents[path]
            assert git_blob_sha1(content) == info["blob_sha"]
            assert hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest() == info["blob_sha"]
    assert m["files"]["assets/image.bin"] == {**m["files"]["assets/image.bin"], "exported": False, "reason": "binary"}
    assert m["files"]["characters/index.json"]["class"] == "index"
    assert m["counts"] == {"listed": 7, "exported": 6, "bootstrap": 3, "index": 1, "lazy": 2}


def test_oversized_and_symlinked_files_listed_but_not_exported(snapenv: GitEnv):
    snapenv._reset_seed()
    blob = git(snapenv.seed, "hash-object", "-w", "--stdin", input=b"docs/rules.md")
    git(snapenv.seed, "update-index", "--add", "--cacheinfo", f"120000,{blob},docs/link.md")
    (snapenv.seed / "docs" / "big.md").write_bytes(b"x" * 5000)
    git(snapenv.seed, "add", "docs/big.md")
    git(snapenv.seed, "commit", "--quiet", "-m", "link and big file")
    git(snapenv.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
    snapenv.with_repo_config(export=EXPORT.model_copy(update={"max_file_bytes": 1000}))
    out = snapenv.repo.refresh()
    assert out.ok, out.error
    files = _manifest(snapenv)["files"]
    assert files["docs/link.md"]["reason"] == "symlink"
    assert files["docs/big.md"]["reason"] == "too_large"
    assert "docs/link.md" not in _exported(snapenv)


def test_repository_file_named_like_manifest_is_not_exported(snapenv: GitEnv):
    snapenv.with_repo_config(export=EXPORT.model_copy(update={"bootstrap": ["*.json", "MANIFEST.md"]}))
    out = snapenv.repo.refresh()
    assert out.ok, out.error
    m = _manifest(snapenv)
    assert m["files"]["snapshot.json"]["reason"] == "unsupported_path"
    assert m["state"] == "complete"  # the real manifest was not overwritten by the repo file


def test_total_size_limit(snapenv: GitEnv):
    snapenv.with_repo_config(export=EXPORT.model_copy(update={"max_total_bytes": 10}))
    out = snapenv.repo.refresh()
    assert not out.ok and out.error.code == "snapshot_failed"


# ------------------------------------------------------------------ export


def test_refresh_exports_files_and_manifest(snapenv: GitEnv):
    out = snapenv.repo.refresh()
    assert out.ok, out.error
    assert out.commit == snapenv.head()
    assert (out.exported, out.uploaded, out.unchanged, out.deleted) == (6, 6, 0, 0)
    exported = _exported(snapenv)
    assert exported["records/juan-23.md"] == b"Juan\n"
    assert "records/private-notes.md" not in exported and "src/unmatched.py" not in exported
    m = _manifest(snapenv)
    assert m["state"] == "complete" and m["commit"] == out.commit and m["generation_id"] == out.generation_id
    assert m["generated_at"].endswith("Z")
    for path, content in exported.items():
        assert git_blob_sha1(content) == m["files"][path]["blob_sha"]
    assert "Juan" not in json.dumps(m)  # manifest never contains file contents


def test_second_refresh_updates_only_changes(snapenv: GitEnv):
    first = snapenv.repo.refresh()
    snapenv._reset_seed()
    snapenv._write({"records/juan-23.md": "Juan v2\n", "characters/alice.md": None, "records/new.md": "New\n"})
    git(snapenv.seed, "add", "-A")
    git(snapenv.seed, "commit", "--quiet", "-m", "edit")
    git(snapenv.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
    second = snapenv.repo.refresh()
    assert second.ok
    assert (second.uploaded, second.deleted, second.unchanged) == (2, 1, 4)
    assert second.previous_commit == first.commit
    exported = _exported(snapenv)
    assert exported["records/juan-23.md"] == b"Juan v2\n"
    assert "characters/alice.md" not in exported
    assert not (snapenv.export_root / "characters" / "alice.md").exists()


def test_manifest_written_after_files(snapenv: GitEnv):
    recording = RecordingStore(LocalDirectorySnapshotStore(snapenv.export_root))
    snapenv.store_factory = lambda repo: recording
    assert snapenv.repo.refresh().ok
    ops = recording.ops
    assert ops[0] == ("put_snapshot", "updating")
    assert ops[-1] == ("put_snapshot", "complete")
    assert all(op[0] in ("put_file", "delete_file") for op in ops[1:-1])
    assert len(ops) == 2 + 6


def test_no_commit_mixing_when_remote_moves_during_export(snapenv: GitEnv):
    target = snapenv.head()
    moved = {}

    def push_newer_commit(path):
        if not moved:
            moved["sha"] = snapenv.advance_remote("records/juan-23.md", "changed mid-export\n")

    recording = RecordingStore(LocalDirectorySnapshotStore(snapenv.export_root), hooks={"put_file": push_newer_commit})
    snapenv.store_factory = lambda repo: recording
    out = snapenv.repo.refresh()
    assert out.ok and out.commit == target != moved["sha"]
    m = _manifest(snapenv)
    assert m["commit"] == target
    for path, content in _exported(snapenv).items():
        # Every exported byte is the blob recorded for the target commit.
        assert git_blob_sha1(content) == m["files"][path]["blob_sha"]
        assert m["files"][path]["blob_sha"] == git(snapenv.origin, "rev-parse", f"{target}:{path}")
    assert _exported(snapenv)["records/juan-23.md"] == b"Juan\n"
    assert "concurrent.md" not in _exported(snapenv)


def test_failed_export_leaves_manifest_marked_updating_and_retry_completes(snapenv: GitEnv):
    assert snapenv.repo.refresh().ok
    before = _manifest(snapenv)
    snapenv.advance_remote("records/juan-23.md", "v2\n")
    failing = FailingStore(LocalDirectorySnapshotStore(snapenv.export_root))
    snapenv.store_factory = lambda repo: failing
    out = snapenv.repo.refresh()
    assert not out.ok and out.error.code == "snapshot_failed"
    assert "Drive unavailable" in out.error.message
    marker = _manifest(snapenv)
    assert marker["state"] == "updating"
    assert marker["previous_commit"] == before["commit"]
    assert marker["target_commit"] == snapenv.head()
    assert "files" not in marker  # an in-flux snapshot claims no file list

    snapenv.store_factory = lambda repo: LocalDirectorySnapshotStore(snapenv.export_root)
    retry = snapenv.repo.refresh()
    assert retry.ok and _manifest(snapenv)["state"] == "complete"
    assert retry.previous_commit == before["commit"]


def test_refresh_of_commit_not_on_branch_rejected(snapenv: GitEnv):
    out = snapenv.repo.refresh(commit="f" * 40)
    assert not out.ok


def test_refresh_without_export_config(gitenv: GitEnv, tmp_path):
    gitenv.store_factory = lambda repo: LocalDirectorySnapshotStore(tmp_path / "x")
    out = gitenv.repo.refresh()
    assert out.error.code == "snapshot_not_configured"


def test_local_store_refuses_traversal(tmp_path):
    store = LocalDirectorySnapshotStore(tmp_path / "root")
    meta = FileMetadata("0" * 40, 1, "lazy", "text/plain")
    for bad in ("../x.md", "/abs.md", "a/../../x.md", "snapshot.json", "a\\b.md", ""):
        with pytest.raises(SnapshotError):
            store.put_file(bad, b"x", meta)
    assert not (tmp_path / "x.md").exists()


# ---------------------------------------------------- publish + snapshot


def test_snapshot_refreshed_after_publication(snapenv: GitEnv):
    assert snapenv.repo.refresh().ok
    base, patch = snapenv.make_patch({"records/juan-23.md": "Juan published\n"})
    out = snapenv.repo.publish_and_refresh(snapenv.artifact(patch, base))
    assert out.ok and out.git_publish == "success"
    assert out.snapshot_refresh == "success" and out.snapshot_commit == out.new_sha
    assert _manifest(snapenv)["commit"] == out.new_sha
    assert _exported(snapenv)["records/juan-23.md"] == b"Juan published\n"
    assert "Drive snapshot updated" in out.message


def test_snapshot_failure_after_publication_keeps_git_success(snapenv: GitEnv):
    snapenv.store_factory = lambda repo: FailingStore(LocalDirectorySnapshotStore(snapenv.export_root))
    base, patch = snapenv.make_patch({"records/juan-23.md": "Juan published\n"})
    out = snapenv.repo.publish_and_refresh(snapenv.artifact(patch, base))
    assert out.ok and out.git_publish == "success"
    assert out.new_sha == snapenv.head()
    assert out.snapshot_refresh == "failed"
    assert "Drive unavailable" in out.snapshot_error
    assert out.error is None
    assert "FAILED" in out.message


def test_snapshot_store_that_cannot_open_keeps_git_success(snapenv: GitEnv):
    def broken(repo):
        raise RuntimeError("no credentials")

    snapenv.store_factory = broken
    base, patch = snapenv.make_patch({"a.md": "a\n"})
    out = snapenv.repo.publish_and_refresh(snapenv.artifact(patch, base))
    assert out.git_publish == "success" and out.snapshot_refresh == "failed"
    assert "no credentials" in out.snapshot_error


def test_status_reports_snapshot(snapenv: GitEnv):
    snapenv.repo.refresh()
    status = snapenv.repo.status()
    assert status.snapshot.state == "complete"
    assert status.snapshot.commit == snapenv.head()
    assert status.snapshot.file_count == 6

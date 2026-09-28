"""Google Drive transport against an in-memory fake Drive (no network)."""

from __future__ import annotations

import itertools
import json
from unittest.mock import MagicMock

import pytest

from conftest import BRANCH, GitEnv, git
from git_bridge.config import ExportConfig
from git_bridge.drive import (
    FOLDER_MIME,
    DriveFile,
    GoogleDriveApi,
    GoogleDriveSnapshotStore,
    decode_path,
    encode_path,
    load_credentials,
)
from git_bridge.errors import ConfigError, SnapshotError
from git_bridge.store import FileMetadata, git_blob_sha1


class FakeDriveApi:
    """Implements the DriveApi protocol in memory, like drive.file scope:
    only files created through this API exist."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.calls: list[str] = []
        self._ids = (f"id{i}" for i in itertools.count(1))

    def find_folder(self, name, parent_id):
        self.calls.append("find_folder")
        for fid, it in self.items.items():
            if it["folder"] and it["name"] == name and it["parent"] == parent_id and not it["trashed"]:
                return fid
        return None

    def create_folder(self, name, parent_id, props):
        self.calls.append("create_folder")
        fid = next(self._ids)
        self.items[fid] = dict(name=name, parent=parent_id, folder=True, props=dict(props), trashed=False, content=b"", mime=FOLDER_MIME)
        return fid

    def list_files(self, props):
        self.calls.append("list_files")
        return [
            DriveFile(fid, it["name"], dict(it["props"]))
            for fid, it in self.items.items()
            if not it["trashed"] and all(it["props"].get(k) == v for k, v in props.items())
        ]

    def create_file(self, name, parent_id, content, mime_type, props):
        self.calls.append("create_file")
        fid = next(self._ids)
        self.items[fid] = dict(name=name, parent=parent_id, folder=False, props=dict(props), trashed=False, content=content, mime=mime_type)
        return fid

    def update_file(self, file_id, content, mime_type, props):
        self.calls.append("update_file")
        it = self.items[file_id]
        it.update(content=content, mime=mime_type)
        it["props"].update(props)

    def trash_file(self, file_id):
        self.calls.append("trash_file")
        self.items[file_id]["trashed"] = True

    def download(self, file_id):
        return self.items[file_id]["content"]

    # helpers for assertions
    def path_of(self, fid: str) -> str:
        parts = []
        while fid != "root":
            it = self.items[fid]
            parts.append(it["name"])
            fid = it["parent"]
        return "/".join(reversed(parts))

    def live_files(self) -> dict[str, dict]:
        return {self.path_of(fid): it for fid, it in self.items.items() if not it["folder"] and not it["trashed"]}


EXPORT = ExportConfig(
    drive_root="ChatGPT/rot3k", bootstrap=["MANIFEST.md", "docs/**/*.md"], lazy=["records/**/*.md"]
)


@pytest.fixture
def drivenv(gitenv: GitEnv):
    gitenv.with_repo_config(export=EXPORT)
    gitenv.advance_remote("records/juan-23.md", "Juan\n")
    fake = FakeDriveApi()
    gitenv.fake = fake
    gitenv.store_factory = lambda repo: GoogleDriveSnapshotStore(fake, repo_key=repo.key, drive_root="ChatGPT/rot3k")
    return gitenv


def test_refresh_creates_mirrored_folders_and_files(drivenv: GitEnv):
    out = drivenv.repo.refresh()
    assert out.ok, out.error
    files = drivenv.fake.live_files()
    assert set(files) == {
        "ChatGPT/rot3k/MANIFEST.md",
        "ChatGPT/rot3k/docs/guide.md",
        "ChatGPT/rot3k/records/juan-23.md",
        "ChatGPT/rot3k/snapshot.json",
    }
    assert files["ChatGPT/rot3k/records/juan-23.md"]["content"] == b"Juan\n"
    assert files["ChatGPT/rot3k/MANIFEST.md"]["mime"] == "text/markdown"
    manifest = json.loads(files["ChatGPT/rot3k/snapshot.json"]["content"])
    assert manifest["state"] == "complete" and manifest["commit"] == drivenv.head()
    for path, info in manifest["files"].items():
        item = drivenv.fake.items[info["drive_file_id"]]
        assert drivenv.fake.path_of(info["drive_file_id"]) == f"ChatGPT/rot3k/{path}"
        assert git_blob_sha1(item["content"]) == info["blob_sha"]
        assert item["props"]["gb_blob"] == info["blob_sha"]
        assert decode_path(item["props"]) == path


def test_second_refresh_updates_in_place_and_skips_unchanged(drivenv: GitEnv):
    drivenv.repo.refresh()
    ids_before = {p: it for p, it in drivenv.fake.live_files().items()}
    id_of = {drivenv.fake.path_of(fid): fid for fid in drivenv.fake.items}
    drivenv.fake.calls.clear()

    drivenv.advance_remote("records/juan-23.md", "Juan v2\n")
    out = drivenv.repo.refresh()
    assert out.ok and (out.uploaded, out.unchanged) == (1, 2)
    after_ids = {drivenv.fake.path_of(fid): fid for fid in drivenv.fake.items}
    assert after_ids == id_of  # same Drive IDs: names and paths stay stable
    assert drivenv.fake.calls.count("create_file") == 0
    assert drivenv.fake.calls.count("update_file") == 3  # one file + snapshot.json twice
    assert drivenv.fake.live_files()["ChatGPT/rot3k/records/juan-23.md"]["content"] == b"Juan v2\n"
    assert set(ids_before) == set(drivenv.fake.live_files())


def test_removed_files_are_trashed_not_destroyed(drivenv: GitEnv):
    drivenv.repo.refresh()
    drivenv._reset_seed()
    drivenv._write({"records/juan-23.md": None})
    git(drivenv.seed, "add", "-A")
    git(drivenv.seed, "commit", "--quiet", "-m", "remove")
    git(drivenv.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
    out = drivenv.repo.refresh()
    assert out.deleted == 1
    trashed = [it for it in drivenv.fake.items.values() if it["trashed"]]
    assert [t["name"] for t in trashed] == ["juan-23.md"]
    assert "ChatGPT/rot3k/records/juan-23.md" not in drivenv.fake.live_files()


def test_state_recovered_from_drive_alone(drivenv: GitEnv):
    """A fresh store (e.g. after a restart) finds its files via appProperties."""
    drivenv.repo.refresh()
    store = GoogleDriveSnapshotStore(drivenv.fake, repo_key="rot3k", drive_root="ChatGPT/rot3k")
    existing = store.existing_files()
    assert set(existing) == {"MANIFEST.md", "docs/guide.md", "records/juan-23.md"}
    assert store.get_snapshot()["commit"] == drivenv.head()


def test_other_repository_files_are_invisible(drivenv: GitEnv):
    drivenv.repo.refresh()
    other = GoogleDriveSnapshotStore(drivenv.fake, repo_key="cyberpunk-tactics", drive_root="ChatGPT/cyberpunk-tactics")
    assert other.existing_files() == {}
    assert other.get_snapshot() is None


def test_duplicate_file_left_by_interrupted_run_is_trashed():
    fake = FakeDriveApi()
    store = GoogleDriveSnapshotStore(fake, repo_key="r", drive_root="root-folder")
    meta = FileMetadata("a" * 40, 1, "lazy", "text/plain")
    store.put_file("x.md", b"1", meta)
    GoogleDriveSnapshotStore(fake, repo_key="r", drive_root="root-folder").put_file("y.md", b"2", meta)
    # simulate a duplicate of x.md
    dup_props = dict(next(it for it in fake.items.values() if it["name"] == "x.md")["props"])
    fake.create_file("x.md", "id1", b"1", "text/plain", dup_props)
    fresh = GoogleDriveSnapshotStore(fake, repo_key="r", drive_root="root-folder")
    assert set(fresh.existing_files()) == {"x.md", "y.md"}
    assert sum(1 for it in fake.items.values() if it["name"] == "x.md" and not it["trashed"]) == 1


def test_drive_failure_during_publication_refresh_keeps_git_success(drivenv: GitEnv):
    def broken_update(*a, **k):
        raise TimeoutError("Drive timed out")

    drivenv.fake.create_file = broken_update
    base, patch = drivenv.make_patch({"records/new.md": "new\n"})
    out = drivenv.repo.publish_and_refresh(drivenv.artifact(patch, base))
    assert out.git_publish == "success" and out.new_sha == drivenv.head()
    assert out.snapshot_refresh == "failed" and "Drive timed out" in out.snapshot_error


def test_publication_refreshes_drive(drivenv: GitEnv):
    drivenv.repo.refresh()
    base, patch = drivenv.make_patch({"records/new.md": "fresh\n"})
    out = drivenv.repo.publish_and_refresh(drivenv.artifact(patch, base))
    assert out.snapshot_refresh == "success"
    files = drivenv.fake.live_files()
    assert files["ChatGPT/rot3k/records/new.md"]["content"] == b"fresh\n"
    assert json.loads(files["ChatGPT/rot3k/snapshot.json"]["content"])["commit"] == out.new_sha


# --------------------------------------------------------- path metadata


@pytest.mark.parametrize(
    "path",
    ["a.md", "docs/" + "x" * 250 + ".md", "日本語/" + "ü" * 120 + ".md", "a/b/c/" * 60 + "z.md"],
)
def test_path_encoding_roundtrip_within_drive_limits(path):
    props = encode_path(path)
    assert decode_path(props) == path
    for key, value in props.items():
        assert len(key.encode()) + len(value.encode()) <= 124
    assert len(props) + 5 <= 30  # leave room for the other properties


def test_path_too_long_rejected():
    with pytest.raises(SnapshotError):
        encode_path("x" * 5000)


# ------------------------------------------------------- real API wrapper


def test_query_construction_and_escaping():
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": [{"id": "f1", "name": "n", "appProperties": {"gb_repo": "r"}}]}
    api = GoogleDriveApi(service)

    api.list_files({"gb_repo": "rot3k", "gb_kind": "file"})
    q = files.list.call_args.kwargs["q"]
    assert "appProperties has { key='gb_repo' and value='rot3k' }" in q
    assert "appProperties has { key='gb_kind' and value='file' }" in q
    assert q.endswith("trashed = false")

    api.find_folder("O'Brien\\x", "root")
    q = files.list.call_args.kwargs["q"]
    assert "name = 'O\\'Brien\\\\x'" in q and "'root' in parents" in q

    api.trash_file("f1")
    assert files.update.call_args.kwargs == {"fileId": "f1", "body": {"trashed": True}, "fields": "id"}


def test_missing_google_token_gives_actionable_error(tmp_path):
    with pytest.raises(ConfigError, match="google-login"):
        load_credentials(tmp_path / "missing.json")

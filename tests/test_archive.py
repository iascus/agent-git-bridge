"""Archive format: one ZIP per repository with a stable name, replaced in place."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from conftest import BRANCH, GitEnv, git
from git_bridge.config import ExportConfig
from git_bridge.drive import GoogleDriveSnapshotStore
from git_bridge.store import LocalDirectorySnapshotStore, git_blob_sha1
from test_drive import FakeDriveApi
from test_snapshot import MANIFEST_MD, _commit

ARCHIVE_EXPORT = ExportConfig(drive_root="ChatGPT/rot3k", manifest="docs/design/MANIFEST.md")


def _unzip(data: bytes) -> tuple[dict, dict[str, bytes], list[zipfile.ZipInfo]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        contents = {i.filename: zf.read(i) for i in infos}
    manifest = json.loads(contents.pop("snapshot.json"))
    return manifest, contents, infos


@pytest.fixture
def archenv(gitenv: GitEnv) -> GitEnv:
    gitenv.with_repo_config(export=ARCHIVE_EXPORT)
    _commit(
        gitenv,
        {
            "docs/design/MANIFEST.md": MANIFEST_MD,
            "characters/alice.md": "Alice\n",
            "records/juan-23.md": "Juan\n",
            "src/app.py": "print()\n",
        },
    )
    gitenv.export_root = gitenv.tmp / "export"
    gitenv.store_factory = lambda repo: LocalDirectorySnapshotStore(gitenv.export_root)
    return gitenv


def _archive(env: GitEnv) -> bytes:
    return (env.export_root / "rot3k-snapshot.zip").read_bytes()


def test_refresh_writes_single_zip_with_manifest_and_files(archenv: GitEnv):
    out = archenv.repo.refresh()
    assert out.ok, out.error
    assert out.archive_name == "rot3k-snapshot.zip" and out.uploaded == 1
    visible = sorted(p.name for p in archenv.export_root.iterdir() if not p.name.startswith("."))
    assert visible == ["rot3k-snapshot.zip"]

    data = _archive(archenv)
    assert out.archive_bytes == len(data)
    manifest, contents, infos = _unzip(data)
    assert infos[0].filename == "snapshot.json"
    assert manifest["state"] == "complete" and manifest["commit"] == archenv.head() == out.commit
    assert manifest["archive"] == "rot3k-snapshot.zip" and manifest["generated_at"]
    assert set(contents) == {"docs/design/MANIFEST.md", "MANIFEST.md", "characters/alice.md", "records/juan-23.md"}
    for path, content in contents.items():
        assert git_blob_sha1(content) == manifest["files"][path]["blob_sha"]
        assert manifest["files"][path]["blob_sha"] == git(archenv.origin, "rev-parse", f"{out.commit}:{path}")
    assert manifest["files"]["characters/alice.md"]["lazy_class"] == "dossiers"
    assert manifest["files"]["docs/gone.md"]["reason"] == "missing"
    assert "src/app.py" not in contents


def test_zip_is_deterministic_and_unchanged_content_is_not_reuploaded(archenv: GitEnv):
    first = archenv.repo.refresh()
    before = _archive(archenv)
    second = archenv.repo.refresh()
    assert second.ok and second.uploaded == 0 and second.unchanged == 1
    assert _archive(archenv) == before
    assert second.generation_id == first.generation_id
    _, _, infos = _unzip(before)
    assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in infos)
    names = [i.filename for i in infos[1:]]
    assert names == sorted(names)


def test_new_commit_replaces_archive(archenv: GitEnv):
    first = archenv.repo.refresh()
    _commit(archenv, {"characters/alice.md": "Alice v2\n"})
    second = archenv.repo.refresh()
    assert second.ok and second.uploaded == 1 and second.previous_commit == first.commit
    manifest, contents, _ = _unzip(_archive(archenv))
    assert manifest["commit"] == second.commit and manifest["previous_commit"] == first.commit
    assert contents["characters/alice.md"] == b"Alice v2\n"


def test_publication_refreshes_archive(archenv: GitEnv):
    archenv.repo.refresh()
    base, patch = archenv.make_patch({"records/juan-23.md": "Juan published\n"})
    out = archenv.repo.publish_and_refresh(archenv.artifact(patch, base))
    assert out.git_publish == "success" and out.snapshot_refresh == "success"
    manifest, contents, _ = _unzip(_archive(archenv))
    assert manifest["commit"] == out.new_sha
    assert contents["records/juan-23.md"] == b"Juan published\n"


def test_status_reports_archive(archenv: GitEnv):
    archenv.repo.refresh()
    snap = archenv.repo.status().snapshot
    assert (snap.state, snap.commit, snap.archive, snap.file_count) == ("complete", archenv.head(), "rot3k-snapshot.zip", 4)


def test_custom_archive_name_and_validation(archenv: GitEnv):
    archenv.with_repo_config(export=ARCHIVE_EXPORT.model_copy(update={"archive_name": "rt3k.zip"}))
    assert archenv.repo.refresh().ok
    assert (archenv.export_root / "rt3k.zip").exists()
    for bad in ("../x.zip", "a/b.zip", "x.tar", ".hidden.zip"):
        with pytest.raises(ValueError):
            ExportConfig(drive_root="x", archive_name=bad)


# ----------------------------------------------------------------- Drive


@pytest.fixture
def drivearch(archenv: GitEnv) -> GitEnv:
    fake = FakeDriveApi()
    archenv.fake = fake
    archenv.store_factory = lambda repo: GoogleDriveSnapshotStore(fake, repo_key=repo.key, drive_root="ChatGPT/rot3k")
    return archenv


def test_drive_archive_keeps_stable_file_id_and_name(drivearch: GitEnv):
    drivearch.repo.refresh()
    zips = {fid: it for fid, it in drivearch.fake.items.items() if it["mime"] == "application/zip"}
    assert len(zips) == 1
    (fid, item), = zips.items()
    assert drivearch.fake.path_of(fid) == "ChatGPT/rot3k/rot3k-snapshot.zip"
    assert item["props"]["gb_commit"] == drivearch.head()

    _commit(drivearch, {"characters/alice.md": "changed\n"})
    out = drivearch.repo.refresh()
    assert out.uploaded == 1
    zips_after = {f: it for f, it in drivearch.fake.items.items() if it["mime"] == "application/zip" and not it["trashed"]}
    assert list(zips_after) == [fid]  # same Drive file, content replaced in place
    assert _unzip(zips_after[fid]["content"])[0]["commit"] == out.commit


def test_switch_from_files_format_trashes_per_file_exports(drivearch: GitEnv):
    drivearch.with_repo_config(export=ARCHIVE_EXPORT.model_copy(update={"format": "files"}))
    assert drivearch.repo.refresh().ok
    per_file = [it for it in drivearch.fake.items.values() if not it["folder"]]
    assert len(per_file) == 5  # 4 files + snapshot.json

    drivearch.with_repo_config(export=ARCHIVE_EXPORT)
    out = drivearch.repo.refresh()
    assert out.ok and out.deleted == 4
    live = drivearch.fake.live_files()
    assert set(live) == {"ChatGPT/rot3k/rot3k-snapshot.zip"}
    live_folders = {drivearch.fake.path_of(f) for f, it in drivearch.fake.items.items() if it["folder"] and not it["trashed"]}
    assert live_folders == {"ChatGPT", "ChatGPT/rot3k"}


def test_failed_upload_keeps_previous_archive(drivearch: GitEnv):
    first = drivearch.repo.refresh()

    def broken(*a, **k):
        raise TimeoutError("Drive timed out")

    drivearch.fake.update_file = broken
    _commit(drivearch, {"characters/alice.md": "changed\n"})
    out = drivearch.repo.refresh()
    assert not out.ok and "Drive timed out" in out.error.message
    (item,) = [it for it in drivearch.fake.items.values() if it["mime"] == "application/zip"]
    assert _unzip(item["content"])[0]["commit"] == first.commit  # old snapshot intact and consistent

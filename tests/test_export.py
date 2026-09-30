"""Refresh end to end: stores, generation consistency, push/rebase flows."""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from conftest import BRANCH, INTEGRATION, GitEnv, artifact_zip, git
from git_bridge.artifact import parse_artifact
from git_bridge.drive import FOLDER_MIME, DriveFile, GoogleDriveApi, GoogleDriveSnapshotStore, load_credentials
from git_bridge.errors import ConfigError
from git_bridge.store import LocalDirectorySnapshotStore, SnapshotStore
from test_snapshot import BASE_FILES, BASE_LISTED, EXPORT, MANIFEST_PATH, manifest_md, reconstruct, selected_tree, unzip


class RecordingStore(SnapshotStore):
    def __init__(self, inner: SnapshotStore, fail_on: str | None = None) -> None:
        self.inner, self.fail_on, self.ops = inner, fail_on, []

    def put_artifact(self, role, name, data, mime_type, info):
        if role == self.fail_on:
            raise ConnectionError(f"Drive unavailable while writing {role}")
        self.ops.append((role, name))
        return self.inner.put_artifact(role, name, data, mime_type, info)

    def get_artifact_info(self, role):
        return self.inner.get_artifact_info(role)


@pytest.fixture
def env(gitenv: GitEnv) -> GitEnv:
    base = gitenv.commit_to(INTEGRATION, {MANIFEST_PATH: manifest_md(*BASE_LISTED), **BASE_FILES}, "project base")
    gitenv.set_branch(BRANCH, base)
    gitenv.with_repo_config(export=EXPORT)
    gitenv.export_root = gitenv.tmp / "export"
    gitenv.store_factory = lambda repo: LocalDirectorySnapshotStore(gitenv.export_root)
    return gitenv


def artifacts(env: GitEnv) -> tuple[dict, dict[str, bytes], bytes]:
    manifest, files = unzip((env.export_root / "rot3k-snapshot.zip").read_bytes())
    return manifest, files, (env.export_root / "rot3k-working.diff").read_bytes()


def assert_coherent(env: GitEnv, tmp: Path) -> dict:
    manifest, _, diff = artifacts(env)
    assert manifest["working_diff"]["sha256"] == hashlib.sha256(diff).hexdigest()
    assert f"# generation: {manifest['generation_id']}".encode() in diff
    rebuilt = reconstruct(tmp, (env.export_root / "rot3k-snapshot.zip").read_bytes(), diff)
    assert rebuilt == selected_tree(env, manifest["working_commit"])
    return manifest


# ------------------------------------------------------------------ refresh


def test_refresh_exports_both_artifacts_from_one_resolved_pair(env: GitEnv, tmp_path):
    env.commit_to(BRANCH, {"AGENTS.md": "working\n"})
    out = env.repo.refresh()
    assert out.ok, out.error
    assert (out.integration_commit, out.working_commit) == (env.head(INTEGRATION), env.head(BRANCH))
    assert out.uploaded and out.file_count == 5
    assert out.working_diff.filename == "rot3k-working.diff" and not out.working_diff.empty
    visible = sorted(p.name for p in env.export_root.iterdir() if not p.name.startswith("."))
    assert visible == ["rot3k-snapshot.zip", "rot3k-working.diff"]
    m = assert_coherent(env, tmp_path)
    assert m["generation_id"] == out.generation_id
    assert (m["integration_commit"], m["working_commit"]) == (out.integration_commit, out.working_commit)
    assert "design-docs" in out.message and "main" in out.message


def test_overlay_is_written_before_snapshot(env: GitEnv):
    recording = RecordingStore(LocalDirectorySnapshotStore(env.export_root))
    env.store_factory = lambda repo: recording
    assert env.repo.refresh().ok
    assert [role for role, _ in recording.ops] == ["working_diff", "snapshot"]


def test_interrupted_refresh_is_detectable_and_retry_repairs(env: GitEnv, tmp_path):
    assert env.repo.refresh().ok
    env.commit_to(BRANCH, {"AGENTS.md": "newer\n"})
    env.store_factory = lambda repo: RecordingStore(LocalDirectorySnapshotStore(env.export_root), fail_on="snapshot")
    out = env.repo.refresh()
    assert not out.ok and "Drive unavailable" in out.error.message
    # New overlay, old snapshot: the pinned sha256 no longer matches -> reader refuses.
    manifest, _, diff = artifacts(env)
    assert manifest["working_diff"]["sha256"] != hashlib.sha256(diff).hexdigest()
    env.store_factory = lambda repo: LocalDirectorySnapshotStore(env.export_root)
    assert env.repo.status().snapshot.consistent is False
    assert env.repo.refresh().ok
    assert assert_coherent(env, tmp_path)["working_commit"] == env.head(BRANCH)
    assert env.repo.status().snapshot.consistent is True


def test_unchanged_pair_is_not_reuploaded(env: GitEnv):
    first = env.repo.refresh()
    before = {p.name: p.read_bytes() for p in env.export_root.iterdir()}
    second = env.repo.refresh()
    assert second.ok and not second.uploaded
    assert second.generation_id == first.generation_id
    assert {p.name: p.read_bytes() for p in env.export_root.iterdir()} == before


def test_main_moving_alone_triggers_new_generation(env: GitEnv, tmp_path):
    first = env.repo.refresh()
    env.commit_to(INTEGRATION, {"docs/design/RULES.md": "main moved\n"})
    second = env.repo.refresh()
    assert second.uploaded and second.integration_commit != first.integration_commit
    assert second.working_commit == first.working_commit
    assert (second.working_ahead_by, second.working_behind_by) == (0, 1)
    assert_coherent(env, tmp_path)


# ------------------------------------------------------------- push flows


def test_refresh_after_push_uses_new_working_head(env: GitEnv, tmp_path):
    env.repo.refresh()
    base, patch = env.make_patch({"docs/design/RULES.md": "Rule one.\nRule two.\nRule three.\nPushed rule.\n"})
    out = env.repo.push_and_refresh(env.artifact(patch, base))
    assert out.ok and out.git_push == "success" and out.snapshot_refresh == "success"
    m = assert_coherent(env, tmp_path)
    assert m["working_commit"] == out.new_sha
    assert m["integration_commit"] == env.head(INTEGRATION) != out.new_sha
    assert out.snapshot_generation_id == m["generation_id"]
    assert "+Pushed rule." in artifacts(env)[2].decode()


def test_refresh_after_push_sees_concurrently_advanced_main(env: GitEnv, tmp_path):
    base, patch = env.make_patch({"AGENTS.md": "pushed\n"})
    new_main = env.commit_to(INTEGRATION, {"docs/design/LORE.md": "main rewrote the lore\n"})
    out = env.repo.push_and_refresh(env.artifact(patch, base))
    assert out.snapshot_refresh == "success"
    m = assert_coherent(env, tmp_path)
    assert (m["integration_commit"], m["working_commit"]) == (new_main, out.new_sha)
    assert (m["working_ahead_by"], m["working_behind_by"]) == (1, 1)


def test_push_is_validated_against_working_commit_from_snapshot(env: GitEnv):
    env.commit_to(INTEGRATION, {"AGENTS.md": "main moved\n"})
    env.repo.refresh()
    manifest, _, _ = artifacts(env)
    base, patch = env.make_patch({"docs/design/RULES.md": "edited\n"})
    assert base == manifest["working_commit"] != manifest["integration_commit"]
    assert env.repo.push(env.artifact(patch, manifest["working_commit"])).ok
    rejected = env.repo.push(env.artifact(patch, manifest["integration_commit"]))
    assert rejected.error.code == "remote_changed"


def test_external_rebase_is_reflected_by_next_refresh(env: GitEnv, tmp_path):
    env.commit_to(BRANCH, {"docs/characters/liu-bei.md": "Liu Bei, revised\n"})
    env.commit_to(INTEGRATION, {"AGENTS.md": "Agents v2 (main)\n"})
    stale = env.repo.refresh()
    assert (stale.working_ahead_by, stale.working_behind_by) == (1, 1)
    assert "Agents v2" in artifacts(env)[2].decode()  # overlay undoes main's change

    # The user rebases design-docs onto main in VS Code (simulated).
    rebased = env.commit_to(INTEGRATION, {"docs/characters/liu-bei.md": "Liu Bei, revised\n"}, "rebased work")
    env.set_branch(INTEGRATION, git(env.origin, "rev-parse", f"{rebased}^"))
    env.set_branch(BRANCH, rebased)
    fresh = env.repo.refresh()
    assert (fresh.working_ahead_by, fresh.working_behind_by) == (1, 0)
    diff = artifacts(env)[2].decode()
    assert "Agents v2" not in diff and "+Liu Bei, revised" in diff
    assert_coherent(env, tmp_path)

    # Rebased and merged: working equals integration -> empty overlay.
    env.set_branch(INTEGRATION, rebased)
    merged = env.repo.refresh()
    assert merged.working_diff.empty
    assert_coherent(env, tmp_path)


# ------------------------------------------------------ project generality


def test_other_branch_names_and_renamed_repository(env: GitEnv, tmp_path):
    env.set_branch("trunk", env.head(INTEGRATION))
    env.set_branch("docs", env.head(BRANCH))
    env.with_repo_config(
        github_repo="iascus/rot3k-renamed", former_github_repos=["iascus/rot3k"],
        integration_branch="trunk", working_branch="docs",
    )
    env.commit_to("docs", {"AGENTS.md": "docs branch\n"})
    out = env.repo.refresh()
    assert out.ok and (out.integration_branch, out.working_branch) == ("trunk", "docs")
    m = assert_coherent(env, tmp_path)
    assert (m["integration_branch"], m["working_branch"]) == ("trunk", "docs")
    assert (m["repository"], m["project_key"]) == ("iascus/rot3k-renamed", "rot3k")
    assert (env.export_root / "rot3k-snapshot.zip").exists()  # names follow the key, not GitHub
    # Pushes go to "docs" and accept the former repository name.
    head = env.head("docs")
    env._reset_seed("docs")
    (env.seed / "AGENTS.md").write_bytes(b"pushed to docs\n")
    git(env.seed, "add", "-A")
    patch = git(env.seed, "diff", "--cached").encode() + b"\n"
    git(env.seed, "reset", "--quiet", "--hard")
    art = parse_artifact(artifact_zip(patch, head, repository="iascus/rot3k", branch="docs"), env.limits)
    out = env.repo.push(art)
    assert out.ok and env.head("docs") == out.new_sha and env.head("trunk") != out.new_sha


def test_custom_artifact_names(env: GitEnv):
    env.with_repo_config(export=EXPORT.model_copy(update={"snapshot_name": "rt3k.zip", "working_diff_name": "rt3k-dd.diff"}))
    out = env.repo.refresh()
    assert out.snapshot_name == "rt3k.zip" and out.working_diff.filename == "rt3k-dd.diff"
    assert unzip((env.export_root / "rt3k.zip").read_bytes())[0]["working_diff"]["filename"] == "rt3k-dd.diff"


def test_status_reports_both_heads_and_snapshot(env: GitEnv):
    env.commit_to(BRANCH, {"AGENTS.md": "w\n"})
    env.repo.refresh()
    st = env.repo.status()
    assert (st.integration.commit, st.working.commit) == (env.head(INTEGRATION), env.head(BRANCH))
    assert (st.working_ahead_by, st.working_behind_by) == (1, 0)
    snap = st.snapshot
    assert snap.consistent and snap.working_commit == env.head(BRANCH) and snap.file_count == 5
    assert (snap.snapshot_name, snap.working_diff_name) == ("rot3k-snapshot.zip", "rot3k-working.diff")


def test_refresh_without_export_config(gitenv: GitEnv, tmp_path):
    gitenv.store_factory = lambda repo: LocalDirectorySnapshotStore(tmp_path / "x")
    assert gitenv.repo.refresh().error.code == "snapshot_not_configured"


def test_push_without_snapshot_transport_reports_not_configured(env: GitEnv):
    env.store_factory = None
    base, patch = env.make_patch({"a.md": "a\n"})
    out = env.repo.push_and_refresh(env.artifact(patch, base))
    assert out.git_push == "success" and out.snapshot_refresh == "not_configured"


def test_snapshot_failure_after_push_keeps_git_success(env: GitEnv):
    env.store_factory = lambda repo: RecordingStore(LocalDirectorySnapshotStore(env.export_root), fail_on="working_diff")
    base, patch = env.make_patch({"a.md": "a\n"})
    out = env.repo.push_and_refresh(env.artifact(patch, base))
    assert out.ok and out.git_push == "success" and out.new_sha == env.head()
    assert out.snapshot_refresh == "failed" and "Drive unavailable" in out.snapshot_error
    assert "FAILED" in out.message


# -------------------------------------------------------------- Google Drive


class FakeDriveApi:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self._ids = (f"id{i}" for i in itertools.count(1))

    def find_folder(self, name, parent_id):
        for fid, it in self.items.items():
            if it["folder"] and it["name"] == name and it["parent"] == parent_id and not it["trashed"]:
                return fid
        return None

    def create_folder(self, name, parent_id, props):
        fid = next(self._ids)
        self.items[fid] = dict(name=name, parent=parent_id, folder=True, props=dict(props), trashed=False, content=b"", mime=FOLDER_MIME)
        return fid

    def list_files(self, props):
        return [
            DriveFile(fid, it["name"], dict(it["props"]))
            for fid, it in self.items.items()
            if not it["trashed"] and all(it["props"].get(k) == v for k, v in props.items())
        ]

    def create_file(self, name, parent_id, content, mime_type, props):
        fid = next(self._ids)
        self.items[fid] = dict(name=name, parent=parent_id, folder=False, props=dict(props), trashed=False, content=content, mime=mime_type)
        return fid

    def update_file(self, file_id, content, mime_type, props, name=None):
        it = self.items[file_id]
        it.update(content=content, mime=mime_type)
        it["props"].update(props)
        if name is not None:
            it["name"] = name

    def trash_file(self, file_id):
        self.items[file_id]["trashed"] = True

    def download(self, file_id):
        return self.items[file_id]["content"]

    def path_of(self, fid):
        parts = []
        while fid != "root":
            parts.append(self.items[fid]["name"])
            fid = self.items[fid]["parent"]
        return "/".join(reversed(parts))

    def live(self):
        return {self.path_of(f): (f, it) for f, it in self.items.items() if not it["folder"] and not it["trashed"]}


@pytest.fixture
def drive_env(env: GitEnv) -> GitEnv:
    fake = FakeDriveApi()
    env.fake = fake
    env.store_factory = lambda repo: GoogleDriveSnapshotStore(fake, repo_key=repo.key, drive_root="ChatGPT/rot3k")
    return env


def test_drive_artifacts_keep_stable_ids_across_generations(drive_env: GitEnv):
    drive_env.repo.refresh()
    live = drive_env.fake.live()
    assert set(live) == {"ChatGPT/rot3k/rot3k-snapshot.zip", "ChatGPT/rot3k/rot3k-working.diff"}
    ids = {path: fid for path, (fid, _) in live.items()}
    drive_env.commit_to(BRANCH, {"AGENTS.md": "changed\n"})
    out = drive_env.repo.refresh()
    assert out.uploaded
    live2 = drive_env.fake.live()
    assert {path: fid for path, (fid, _) in live2.items()} == ids
    snap = unzip(live2["ChatGPT/rot3k/rot3k-snapshot.zip"][1]["content"])[0]
    diff = live2["ChatGPT/rot3k/rot3k-working.diff"][1]["content"]
    assert snap["working_diff"]["sha256"] == hashlib.sha256(diff).hexdigest()
    assert snap["working_commit"] == out.working_commit


def test_drive_reuses_snapshot_zip_from_previous_version(drive_env: GitEnv):
    """The v1 ZIP (kind 'archive') keeps its Drive file ID after the upgrade."""
    store = GoogleDriveSnapshotStore(drive_env.fake, repo_key="rot3k", drive_root="ChatGPT/rot3k")
    old_id = drive_env.fake.create_file("rot3k-snapshot.zip", store._root(), b"v1", "application/zip", {"gb_repo": "rot3k", "gb_kind": "archive"})
    drive_env.repo.refresh()
    fid, item = drive_env.fake.live()["ChatGPT/rot3k/rot3k-snapshot.zip"]
    assert fid == old_id and unzip(item["content"])[0]["format_version"] == 2


def test_drive_projects_are_isolated(drive_env: GitEnv):
    drive_env.repo.refresh()
    other = GoogleDriveSnapshotStore(drive_env.fake, repo_key="cyberpunk-tactics", drive_root="ChatGPT/cyberpunk-tactics")
    assert other.get_artifact_info("snapshot") is None and other.get_artifact_info("working_diff") is None


def test_drive_query_construction_and_escaping():
    service = MagicMock()
    files = service.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    api = GoogleDriveApi(service)
    api.list_files({"gb_repo": "rot3k", "gb_kind": "archive"})
    q = files.list.call_args.kwargs["q"]
    assert "appProperties has { key='gb_repo' and value='rot3k' }" in q and q.endswith("trashed = false")
    api.find_folder("O'Brien\\x", "root")
    assert "name = 'O\\'Brien\\\\x'" in files.list.call_args.kwargs["q"]


def test_missing_google_token_gives_actionable_error(tmp_path):
    with pytest.raises(ConfigError, match="google-login"):
        load_credentials(tmp_path / "missing.json")


# ----------------------------------------------------------------------- API


def test_refresh_endpoint_reports_both_states(env: GitEnv):
    from fastapi.testclient import TestClient

    from git_bridge.api import create_app
    from git_bridge.config import ServerConfig

    token_file = env.tmp / "bearer"
    token_file.write_text("t" * 48)
    settings = env.settings.model_copy(update={"server": ServerConfig(bearer_token_file=token_file)})
    client = TestClient(create_app(settings, bridge=env.bridge))
    body = client.post("/repos/rot3k/refresh", headers={"Authorization": "Bearer " + "t" * 48}).json()
    assert body["ok"] and body["integration_branch"] == "main" and body["working_branch"] == "design-docs"
    assert body["integration_commit"] == env.head(INTEGRATION) and body["working_commit"] == env.head(BRANCH)
    assert body["working_diff"]["empty"] is True

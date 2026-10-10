"""Optional additional working branches: each gets its own independent
snapshot + overlay pair, created only when the branch actually exists on
the remote, alongside the default pair which is byte-for-byte unaffected
by this feature when no additional branch is configured."""

from __future__ import annotations

import hashlib
import json

import pytest

from conftest import BRANCH, INTEGRATION, GitEnv, make_zip
from git_bridge.artifact import parse_artifact
from git_bridge.config import PullRequestConfig, RepositoryConfig
from git_bridge.store import LocalDirectorySnapshotStore, SnapshotStore
from test_snapshot import BASE_FILES, BASE_LISTED, EXPORT, MANIFEST_PATH, manifest_md, png, reconstruct, selected_tree, unzip

GFX = "gfx-assets"


class FailingForBranch(SnapshotStore):
    """Fails every write for exactly one branch; others pass through."""

    def __init__(self, inner: SnapshotStore, *, fail_branch: str, this_branch: str | None) -> None:
        self.inner, self.fail_branch, self.this_branch = inner, fail_branch, this_branch

    def put_artifact(self, role, name, data, mime_type, info):
        if self.this_branch == self.fail_branch:
            raise ConnectionError(f"Drive unavailable for {self.this_branch}")
        return self.inner.put_artifact(role, name, data, mime_type, info)

    def get_artifact_info(self, role):
        return self.inner.get_artifact_info(role)


@pytest.fixture
def env(gitenv: GitEnv) -> GitEnv:
    base = gitenv.commit_to(
        INTEGRATION,
        {MANIFEST_PATH: manifest_md(*BASE_LISTED, roots=("assets/gfx/",)), **BASE_FILES},
        "project base",
    )
    gitenv.set_branch(BRANCH, base)
    gitenv.with_repo_config(export=EXPORT, additional_working_branches=[GFX])
    gitenv.export_root = gitenv.tmp / "export"
    gitenv.store_factory = lambda repo, branch=None: LocalDirectorySnapshotStore(gitenv.export_root, branch=branch)
    return gitenv


def names(branch: str | None = None) -> tuple[str, str]:
    prefix = "rot3k" if branch is None else f"rot3k-{branch}"
    return f"{prefix}-snapshot.zip", f"{prefix}-working.diff"


def artifacts(env: GitEnv, branch: str | None = None) -> tuple[dict, dict[str, bytes], bytes]:
    snap_name, diff_name = names(branch)
    manifest, files = unzip((env.export_root / snap_name).read_bytes())
    return manifest, files, (env.export_root / diff_name).read_bytes()


# ---------------------------------------------------------- default unaffected


def test_default_push_with_explicit_branch_is_unaffected(env: GitEnv):
    """An old push ZIP naming the default branch explicitly still works
    exactly as before, even though additional_working_branches is set."""
    base, patch = env.make_patch({"AGENTS.md": "changed\n"})
    out = env.repo.push(env.artifact(patch, base, branch=BRANCH))
    assert out.ok, out.error
    assert out.branch == BRANCH and env.head(BRANCH) == out.new_sha


def test_refresh_without_gfx_branch_still_produces_default_pair_only(env: GitEnv, tmp_path):
    out = env.repo.refresh()
    assert out.ok, out.error
    assert out.additional_branches[GFX].state == "skipped_missing"
    visible = sorted(p.name for p in env.export_root.iterdir() if not p.name.startswith("."))
    assert visible == list(names())  # no gfx-assets files created
    manifest, _, diff = artifacts(env)
    assert reconstruct(tmp_path, (env.export_root / names()[0]).read_bytes(), diff) == selected_tree(env, env.head(BRANCH))


# --------------------------------------------------------- branch resolution


def test_omitted_branch_selects_default(env: GitEnv):
    """"branch" absent from request.json entirely (not just null)."""
    base, patch = env.make_patch({"AGENTS.md": "omitted\n"})
    req = {
        "format_version": 1,
        "repository": env.github_repo,
        "expected_base_sha": base,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "commit_message": "omit branch",
    }
    assert "branch" not in req
    artifact = parse_artifact(make_zip({"request.json": json.dumps(req).encode(), "changes.patch": patch}), env.limits)
    out = env.repo.push(artifact)
    assert out.ok and out.branch == BRANCH


def test_null_branch_selects_default(env: GitEnv):
    base, patch = env.make_patch({"AGENTS.md": "null\n"})
    out = env.repo.push(env.artifact(patch, base, branch=None))
    assert out.ok and out.branch == BRANCH


def test_omitted_branch_rejects_sha_belonging_only_to_gfx(env: GitEnv):
    env.set_branch(GFX, env.head(INTEGRATION))
    env.commit_to(GFX, {"assets/gfx/new.png": png()})
    gfx_head = env.head(GFX)
    assert gfx_head != env.head(BRANCH)
    _, patch = env.make_patch({"AGENTS.md": "x\n"})
    out = env.repo.push(env.artifact(patch, gfx_head, branch=None))  # omitted -> default, but base is gfx's head
    assert not out.ok and out.error.code == "remote_changed"
    assert env.head(BRANCH) != gfx_head  # nothing applied to the default branch either


def test_explicit_gfx_branch_push_succeeds_and_targets_only_gfx(env: GitEnv):
    env.set_branch(GFX, env.head(INTEGRATION))
    design_docs_before = env.head(BRANCH)
    base, patch = env.make_patch({"assets/gfx/new.png": png()}, binary=True, branch=GFX)
    out = env.repo.push(env.artifact(patch, base, branch=GFX))
    assert out.ok, out.error
    assert out.branch == GFX
    assert env.head(GFX) == out.new_sha
    assert env.head(BRANCH) == design_docs_before  # design-docs untouched


def test_push_rejects_unknown_branch(env: GitEnv):
    base, patch = env.make_patch({"AGENTS.md": "x\n"})
    out = env.repo.push(env.artifact(patch, base, branch="no-such-branch"))
    assert out.error.code == "not_allowed"
    assert "working branch" in out.error.message


def test_push_to_integration_branch_still_refused(env: GitEnv):
    base, patch = env.make_patch({"AGENTS.md": "x\n"})
    out = env.repo.push(env.artifact(patch, base, branch=INTEGRATION))
    assert out.error.code == "not_allowed"


# --------------------------------------------------------------------- refresh


def test_refresh_produces_both_pairs_independently(env: GitEnv, tmp_path):
    env.set_branch(GFX, env.head(INTEGRATION))
    env.commit_to(GFX, {"assets/gfx/logo.png": png(b"gfx")})
    out = env.repo.refresh()
    assert out.ok, out.error
    gfx = out.additional_branches[GFX]
    assert gfx.state == "ok" and gfx.working_commit == env.head(GFX)

    visible = sorted(p.name for p in env.export_root.iterdir() if not p.name.startswith("."))
    assert visible == sorted([*names(), *names(GFX)])

    default_manifest, _, default_diff = artifacts(env)
    gfx_manifest, gfx_files, gfx_diff = artifacts(env, GFX)
    # Independent fingerprints/SHA-256 links; neither pair references the other.
    assert default_manifest["generation_id"] != gfx_manifest["generation_id"]
    assert gfx_manifest["working_diff"]["sha256"] == hashlib.sha256(gfx_diff).hexdigest()
    assert gfx_manifest["working_branch"] == GFX
    # The png only exists on the gfx-assets branch (D), so it is in the
    # overlay's working_files, not in M's (the snapshot ZIP's) own files.
    assert "assets/gfx/logo.png" in gfx_manifest["working_diff"]["working_files"]
    assert "assets/gfx/logo.png" not in gfx_files

    rebuilt = reconstruct(tmp_path, (env.export_root / names(GFX)[0]).read_bytes(), gfx_diff)
    assert rebuilt == selected_tree(env, env.head(GFX))
    assert rebuilt["assets/gfx/logo.png"] == png(b"gfx")


def test_missing_gfx_branch_reports_skipped_and_leaves_no_stale_files(env: GitEnv):
    out = env.repo.refresh()
    assert out.ok
    assert out.additional_branches[GFX].state == "skipped_missing"
    assert not (env.export_root / names(GFX)[0]).exists()
    assert not (env.export_root / names(GFX)[1]).exists()


def test_updating_one_branch_does_not_change_the_other(env: GitEnv, tmp_path):
    env.set_branch(GFX, env.head(INTEGRATION))
    env.commit_to(GFX, {"assets/gfx/a.png": png(b"a")})
    env.repo.refresh()
    default_before = artifacts(env)
    gfx_before = artifacts(env, GFX)

    base, patch = env.make_patch({"AGENTS.md": "design docs only\n"})
    out = env.repo.push_and_refresh(env.artifact(patch, base, branch=BRANCH))
    assert out.ok and out.snapshot_refresh == "success"

    assert artifacts(env, GFX) == gfx_before  # the gfx pair is untouched
    assert artifacts(env) != default_before  # only the default pair advanced
    assert env.head(GFX) == gfx_before[0]["working_commit"]


def test_transport_failure_on_gfx_branch_does_not_fail_default_refresh(env: GitEnv):
    env.set_branch(GFX, env.head(INTEGRATION))
    env.commit_to(GFX, {"assets/gfx/a.png": png(b"a")})
    inner_factory = env.store_factory
    env.store_factory = lambda repo, branch=None: FailingForBranch(inner_factory(repo, branch), fail_branch=GFX, this_branch=branch)

    out = env.repo.refresh()
    assert out.ok, out.error  # the default pair still succeeds
    assert out.uploaded
    gfx = out.additional_branches[GFX]
    assert gfx.state == "failed"
    assert gfx.error is not None and "Drive unavailable" in gfx.error.message
    assert (env.export_root / names()[0]).exists()  # default pair was written
    assert not (env.export_root / names(GFX)[0]).exists()  # gfx pair was not


def test_empty_gfx_root_push_still_refreshes_both_pairs(env: GitEnv):
    """An additional branch that exists but is identical to main (empty
    overlay) must still refresh cleanly."""
    env.set_branch(GFX, env.head(INTEGRATION))
    out = env.repo.refresh()
    assert out.ok
    gfx = out.additional_branches[GFX]
    assert gfx.state == "ok" and gfx.working_diff.empty


# -------------------------------------------------------- push-then-refresh


def test_push_to_gfx_then_refresh_updates_both_pairs_and_opens_its_own_pr(env: GitEnv, tmp_path):
    class FakeGitHub:
        def __init__(self):
            self.calls = []

        def find_open_pull_request(self, repo, head, base):
            self.calls.append(("find", head, base))
            return None

        def create_pull_request(self, repo, head, base, title, body, draft):
            self.calls.append(("create", head, base))
            return {"number": 5, "html_url": f"https://github.com/{repo}/pull/5"}

        def latest_merged_pull_request(self, repo, head, base):
            return None

    env.with_repo_config(pull_request=PullRequestConfig())
    env.github = FakeGitHub()
    env.set_branch(GFX, env.head(INTEGRATION))
    base, patch = env.make_patch({"assets/gfx/new.png": png()}, binary=True, branch=GFX)
    out = env.repo.push_and_refresh(env.artifact(patch, base, branch=GFX))
    assert out.ok and out.snapshot_refresh == "success"
    assert out.pull_request.state == "created"
    assert env.github.calls == [("find", GFX, "main"), ("create", GFX, "main")]
    # Both pairs are now present.
    assert (env.export_root / names()[0]).exists()
    assert (env.export_root / names(GFX)[0]).exists()
    gfx_manifest, gfx_files, gfx_diff = artifacts(env, GFX)
    assert gfx_manifest["working_commit"] == out.new_sha
    # The png was just pushed to gfx-assets (D); main (M) has not moved, so
    # it is in the overlay, not in the snapshot ZIP's own files.
    assert "assets/gfx/new.png" in gfx_manifest["working_diff"]["working_files"]
    assert "assets/gfx/new.png" not in gfx_files
    rebuilt = reconstruct(tmp_path, (env.export_root / names(GFX)[0]).read_bytes(), gfx_diff)
    assert rebuilt["assets/gfx/new.png"] == png()


def test_stale_base_on_gfx_branch_is_rejected_without_touching_design_docs(env: GitEnv):
    env.set_branch(GFX, env.head(INTEGRATION))
    base, patch = env.make_patch({"assets/gfx/new.png": png()}, binary=True, branch=GFX)
    moved = env.advance_remote(branch=GFX)
    design_docs_before = env.head(BRANCH)
    out = env.repo.push(env.artifact(patch, base, branch=GFX))
    assert not out.ok and out.error.code == "remote_changed"
    assert env.head(GFX) == moved
    assert env.head(BRANCH) == design_docs_before


# ------------------------------------------------------------ configuration


def test_additional_branch_overlapping_default_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        RepositoryConfig(
            github_repo="iascus/rot3k",
            local_path=tmp_path / "r",
            integration_branch="main",
            working_branch=BRANCH,
            additional_working_branches=[BRANCH],
        )


def test_additional_branch_overlapping_integration_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        RepositoryConfig(
            github_repo="iascus/rot3k",
            local_path=tmp_path / "r",
            integration_branch="main",
            working_branch=BRANCH,
            additional_working_branches=["main"],
        )


@pytest.mark.parametrize("bad", ["a/b", "../x", "", "x" * 101])
def test_invalid_additional_branch_name_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        RepositoryConfig(
            github_repo="iascus/rot3k",
            local_path=tmp_path / "r",
            integration_branch="main",
            working_branch=BRANCH,
            additional_working_branches=[bad],
        )


def test_all_working_branches_and_known_branches(tmp_path):
    cfg = RepositoryConfig(
        github_repo="iascus/rot3k",
        local_path=tmp_path / "r",
        integration_branch="main",
        working_branch=BRANCH,
        additional_working_branches=[GFX, "poc1"],
    )
    assert cfg.all_working_branches == (BRANCH, GFX, "poc1")
    assert cfg.known_branches == ("main", BRANCH, GFX, "poc1")
    assert cfg.branches == ("main", BRANCH)  # unchanged: the required pair only

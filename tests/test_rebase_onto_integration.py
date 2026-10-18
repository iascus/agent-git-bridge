"""rebase_onto_integration: unconditional rebase of working branches onto the
integration branch's current head on every refresh (never a push). Unlike
rebase_after_squash_merge in test_rebase.py, no GitHub API is involved --
but already-merged history (squash, rebase, or any other merge strategy) is
still detected correctly, by tree equality instead of a PR lookup."""

from __future__ import annotations

import os
import subprocess

import pytest

from conftest import BRANCH, INTEGRATION, GitEnv, git
from git_bridge.store import LocalDirectorySnapshotStore
from test_snapshot import BASE_FILES, BASE_LISTED, EXPORT, MANIFEST_PATH, manifest_md, reconstruct, selected_tree

GFX = "gfx-assets"

SQUASH_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "GitHub",
    "GIT_AUTHOR_EMAIL": "noreply@github.com",
    "GIT_COMMITTER_NAME": "GitHub",
    "GIT_COMMITTER_EMAIL": "noreply@github.com",
}


def _commit_tree(env: GitEnv, tree: str, parent: str, message: str) -> str:
    return subprocess.run(
        ["git", "commit-tree", tree, "-p", parent, "-m", message],
        cwd=env.origin, capture_output=True, env=SQUASH_ENV, check=True,
    ).stdout.decode().strip()


def squash_merge(env: GitEnv, branch: str = BRANCH) -> str:
    """What GitHub's "Squash and merge" does: one new commit on main with
    the branch's current tree. No PR, no GitHub API -- just the result a
    tree-equality check must still recognise."""
    head = env.head(branch)
    tree = git(env.origin, "rev-parse", f"{head}^{{tree}}")
    squash = _commit_tree(env, tree, env.head(INTEGRATION), f"Squashed {branch}")
    env.set_branch(INTEGRATION, squash)
    return squash


def rebase_merge(env: GitEnv, branch: str = BRANCH) -> str:
    """What GitHub's "Rebase and merge" does: each original commit replayed
    as a new commit (new SHA, same tree) directly on main."""
    base = env.head(INTEGRATION)
    parent = base
    for commit in git(env.origin, "rev-list", "--reverse", f"{base}..{env.head(branch)}").splitlines():
        tree = git(env.origin, "rev-parse", f"{commit}^{{tree}}")
        message = git(env.origin, "log", "-1", "--format=%s", commit)
        parent = _commit_tree(env, tree, parent, message)
    env.set_branch(INTEGRATION, parent)
    return parent


@pytest.fixture
def env(gitenv: GitEnv) -> GitEnv:
    base = gitenv.commit_to(INTEGRATION, {MANIFEST_PATH: manifest_md(*BASE_LISTED), **BASE_FILES}, "project base")
    gitenv.set_branch(BRANCH, base)
    gitenv.with_repo_config(export=EXPORT, rebase_onto_integration=True)
    gitenv.export_root = gitenv.tmp / "export"
    gitenv.store_factory = lambda repo, branch=None: LocalDirectorySnapshotStore(gitenv.export_root, branch=branch)
    # Three commits on design-docs, never touching main.
    gitenv.commit_to(BRANCH, {"AGENTS.md": "Agents v2\n"}, "C1")
    gitenv.commit_to(BRANCH, {"AGENTS.md": "Agents v3\n"}, "C2 rewrites C1's line")
    gitenv.commit_to(BRANCH, {"docs/design/RULES.md": "Rules v2\n"}, "C3")
    return gitenv


def assert_exported_state_matches_branches(env: GitEnv, tmp_path):
    snap = (env.export_root / "rot3k-snapshot.zip").read_bytes()
    diff = (env.export_root / "rot3k-working.diff").read_bytes()
    assert reconstruct(tmp_path, snap, diff) == selected_tree(env, env.head(BRANCH))


# ---------------------------------------------------------------- refresh


def test_not_needed_when_integration_not_ahead(env: GitEnv, tmp_path):
    assert env.repo.github is None  # no GitHub client configured or needed
    before = env.head(BRANCH)
    out = env.repo.refresh()
    assert out.ok, out.error
    assert out.rebase.state == "not_needed"
    assert env.head(BRANCH) == before
    assert_exported_state_matches_branches(env, tmp_path)


def test_refresh_rebases_onto_advanced_integration(env: GitEnv, tmp_path):
    env.commit_to(INTEGRATION, {"docs/guide.md": "Guide v2\n"}, "a normal PR on main")
    new_main = env.head(INTEGRATION)
    out = env.repo.refresh()
    assert out.ok, out.error
    r = out.rebase
    assert r.state == "rebased"
    assert r.pull_request is None and r.merged_head is None  # no GitHub, no PR involved
    assert r.replayed_commits == 3  # C1, C2, C3
    assert git(env.origin, "rev-parse", f"{env.head(BRANCH)}~3") == new_main
    assert (out.working_ahead_by, out.working_behind_by) == (3, 0)
    assert out.message.startswith("Rebased design-docs onto main (3 commit(s) replayed).")
    assert_exported_state_matches_branches(env, tmp_path)


def test_rebased_tree_equals_merge_result(env: GitEnv):
    env.commit_to(INTEGRATION, {"docs/guide.md": "Guide v2\n"}, "a normal PR on main")
    before = env.head(BRANCH)
    expected = git(env.origin, "merge-tree", "--write-tree", env.head(INTEGRATION), before).splitlines()[0]
    env.repo.refresh()
    assert git(env.origin, "rev-parse", f"{env.head(BRANCH)}^{{tree}}") == expected


def test_second_refresh_does_not_rebase_again(env: GitEnv):
    env.commit_to(INTEGRATION, {"docs/guide.md": "Guide v2\n"}, "a normal PR on main")
    env.repo.refresh()
    head = env.head(BRANCH)
    out = env.repo.refresh()
    assert out.rebase.state == "not_needed" and env.head(BRANCH) == head


def test_conflict_aborts_whole_refresh_and_exports_nothing(env: GitEnv):
    env.commit_to(INTEGRATION, {"docs/design/RULES.md": "Rules v2 (main, conflicting)\n"}, "conflicting PR on main")
    before = env.head(BRANCH)
    out = env.repo.refresh()
    assert not out.ok
    assert out.error.code == "rebase_failed"
    assert "conflict" in out.error.message
    assert env.head(BRANCH) == before  # working branch untouched
    assert not env.export_root.exists()  # nothing exported, not even the default pair
    work = env.settings.work_dir  # caught by merge-tree before any worktree exists
    assert not work.exists() or not any(work.iterdir())


def test_lease_protects_concurrent_push(env: GitEnv, monkeypatch):
    from git_bridge.repository import Repository

    env.commit_to(INTEGRATION, {"docs/guide.md": "Guide v2\n"}, "a normal PR on main")
    real_push = Repository._push
    concurrent = {}

    def push_after_someone_else(self, sha, branch, *, lease=None):
        if lease is not None and not concurrent:
            concurrent["sha"] = env.commit_to(BRANCH, {"late.md": "late\n"}, "pushed meanwhile")
        return real_push(self, sha, branch, lease=lease)

    monkeypatch.setattr(Repository, "_push", push_after_someone_else)
    out = env.repo.refresh()
    assert not out.ok and out.error.code == "rebase_failed"
    assert "nothing was overwritten" in out.error.message
    assert env.head(BRANCH) == concurrent["sha"]  # the concurrent commit survives
    assert not env.export_root.exists()


# ----------------------------------------------------- already-merged history


def test_detects_squash_merge_without_github_and_rebases_cleanly(env: GitEnv, tmp_path):
    """Reproduces the real cyberpunk-tactics/gfx-assets conflict: a branch
    whose PR was squash-merged must not be replayed commit-by-commit from
    the merge-base (which would conflict with content already on main) --
    it must be recognised as already-merged and simply caught up."""
    squash = squash_merge(env)
    out = env.repo.refresh()
    assert out.ok, out.error
    r = out.rebase
    assert r.state == "rebased"
    assert r.pull_request is None  # no GitHub lookup happened
    assert r.merged_head is not None  # but the squash point was found by tree equality
    assert r.replayed_commits == 0
    assert env.head(BRANCH) == squash == r.new_commit
    assert env.head(INTEGRATION) == squash  # main untouched
    assert out.working_diff.empty
    assert_exported_state_matches_branches(env, tmp_path)


def test_squash_merge_replays_only_post_merge_commits(env: GitEnv, tmp_path):
    squash = squash_merge(env)
    env.commit_to(BRANCH, {"docs/characters/liu-bei.md": "Liu Bei, round two\n"}, "C4 after merge")
    out = env.repo.refresh()
    assert out.ok, out.error
    r = out.rebase
    assert r.state == "rebased" and r.replayed_commits == 1
    assert git(env.origin, "rev-parse", f"{env.head(BRANCH)}^") == squash
    assert_exported_state_matches_branches(env, tmp_path)


def test_rebase_merge_strategy_is_also_detected(env: GitEnv, tmp_path):
    """GitHub's "Rebase and merge" gives each commit a new SHA but the same
    tree as the original -- tree equality catches this too, not just a
    literal single-commit squash."""
    merged = rebase_merge(env)
    out = env.repo.refresh()
    assert out.ok, out.error
    r = out.rebase
    assert r.state == "rebased" and r.replayed_commits == 0
    assert env.head(BRANCH) == merged
    assert_exported_state_matches_branches(env, tmp_path)


def test_additional_branch_squash_merge_is_detected_too(env: GitEnv, tmp_path):
    env.set_branch(GFX, env.head(INTEGRATION))
    env.with_repo_config(additional_working_branches=[GFX])
    env.commit_to(GFX, {"assets/new.txt": "gfx work\n"}, "gfx C1")
    env.commit_to(GFX, {"assets/new2.txt": "gfx work 2\n"}, "gfx C2")
    squash = squash_merge(env, branch=GFX)
    out = env.repo.refresh()
    assert out.ok, out.error
    gfx = out.additional_branches[GFX]
    assert gfx.state == "ok"
    assert gfx.rebase.state == "rebased" and gfx.rebase.replayed_commits == 0
    assert env.head(GFX) == squash


def test_unrelated_integration_changes_still_use_plain_merge_base(env: GitEnv):
    """No tree in main matches any of working's commits (nothing of this
    branch was ever merged) -- falls back to a plain rebase from the
    merge-base, same as before this detection existed."""
    env.commit_to(INTEGRATION, {"docs/guide.md": "Guide v2\n"}, "a normal PR on main, unrelated to design-docs")
    out = env.repo.refresh()
    assert out.ok, out.error
    assert out.rebase.state == "rebased" and out.rebase.merged_head is None and out.rebase.replayed_commits == 3


# ------------------------------------------------------------------ push


def test_push_never_rebases(env: GitEnv):
    env.commit_to(INTEGRATION, {"docs/guide.md": "Guide v2\n"}, "a normal PR on main")
    base, patch = env.make_patch({"docs/characters/liu-bei.md": "More Liu Bei\n"})
    out = env.repo.push_and_refresh(env.artifact(patch, base))
    assert out.ok and out.git_push == "success"
    assert git(env.origin, "rev-parse", f"{out.new_sha}^") == base  # on top of the unrebased branch
    # The post-push refresh (allow_rebase=False) must not have rebased either.
    assert git(env.origin, "rev-parse", f"{env.head(BRANCH)}^") == base


# ------------------------------------------------------- additional branches


def test_additional_branch_is_also_rebased(env: GitEnv):
    env.set_branch(GFX, env.head(INTEGRATION))
    env.with_repo_config(additional_working_branches=[GFX])
    env.commit_to(GFX, {"assets/new.txt": "gfx work\n"}, "gfx C1")
    env.commit_to(INTEGRATION, {"docs/guide.md": "Guide v2\n"}, "a normal PR on main")
    new_main = env.head(INTEGRATION)
    out = env.repo.refresh()
    assert out.ok, out.error
    assert out.rebase.state == "rebased"  # the default pair too
    gfx = out.additional_branches[GFX]
    assert gfx.state == "ok"
    assert gfx.rebase.state == "rebased" and gfx.rebase.replayed_commits == 1
    assert git(env.origin, "rev-parse", f"{env.head(GFX)}^") == new_main


def test_conflict_on_additional_branch_aborts_default_pair_export_too(env: GitEnv):
    """design-docs never touches docs/guide.md, so its own rebase is clean
    and its force push already lands -- but gfx-assets' rebase conflicts, so
    the whole refresh still reports failure and nothing gets exported,
    including the default pair whose git-level rebase already succeeded."""
    env.set_branch(GFX, env.head(INTEGRATION))
    env.with_repo_config(additional_working_branches=[GFX])
    env.commit_to(GFX, {"docs/guide.md": "gfx guide\n"}, "gfx edits guide.md")
    gfx_before = env.head(GFX)
    env.commit_to(INTEGRATION, {"docs/guide.md": "main guide\n"}, "conflicting PR on main")
    new_main = env.head(INTEGRATION)

    out = env.repo.refresh()
    assert not out.ok and out.error.code == "rebase_failed"
    assert env.head(GFX) == gfx_before  # gfx-assets untouched: its rebase conflicted
    assert git(env.origin, "rev-parse", f"{env.head(BRANCH)}~3") == new_main  # design-docs WAS rebased...
    assert not env.export_root.exists()  # ...but nothing was exported for either branch


def test_missing_additional_branch_is_skipped_not_an_error(env: GitEnv, tmp_path):
    env.with_repo_config(additional_working_branches=[GFX])
    env.commit_to(INTEGRATION, {"docs/guide.md": "Guide v2\n"}, "a normal PR on main")
    out = env.repo.refresh()
    assert out.ok, out.error
    assert out.additional_branches[GFX].state == "skipped_missing"
    assert out.rebase.state == "rebased"
    assert_exported_state_matches_branches(env, tmp_path)

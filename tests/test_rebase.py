"""Rebase of the working branch after its PR was squash-merged."""

from __future__ import annotations

import os
import subprocess

import pytest

from conftest import BRANCH, INTEGRATION, GitEnv, git
from git_bridge.errors import NotAllowed
from git_bridge.github import PullRequestError
from git_bridge.repository import Repository
from git_bridge.store import LocalDirectorySnapshotStore
from test_snapshot import BASE_FILES, BASE_LISTED, EXPORT, MANIFEST_PATH, manifest_md, reconstruct, selected_tree

SQUASH_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "GitHub",
    "GIT_AUTHOR_EMAIL": "noreply@github.com",
    "GIT_COMMITTER_NAME": "GitHub",
    "GIT_COMMITTER_EMAIL": "noreply@github.com",
}


class FakeGitHub:
    def __init__(self) -> None:
        self.merged: list[dict] = []
        self.fail = False

    def find_open_pull_request(self, repo, head, base):
        return None

    def create_pull_request(self, repo, head, base, title, body, draft):
        return {"number": 99, "html_url": "u"}

    def latest_merged_pull_request(self, repo, head, base):
        if self.fail:
            raise PullRequestError("GitHub API 503: unavailable")
        return self.merged[-1] if self.merged else None


def squash_merge(env: GitEnv, number: int) -> str:
    """What GitHub does: one new commit on main with the PR head's tree."""
    head = env.head(BRANCH)
    tree = git(env.origin, "rev-parse", f"{head}^{{tree}}")
    squash = subprocess.run(
        ["git", "commit-tree", tree, "-p", env.head(INTEGRATION), "-m", f"Squashed PR (#{number})"],
        cwd=env.origin, capture_output=True, env=SQUASH_ENV, check=True,
    ).stdout.decode().strip()
    env.set_branch(INTEGRATION, squash)
    env.github.merged.append(
        {"number": number, "head": {"sha": head}, "merge_commit_sha": squash, "merged_at": f"2026-09-30T10:{number:02d}:00Z"}
    )
    return squash


@pytest.fixture
def env(gitenv: GitEnv) -> GitEnv:
    base = gitenv.commit_to(INTEGRATION, {MANIFEST_PATH: manifest_md(*BASE_LISTED), **BASE_FILES}, "project base")
    gitenv.set_branch(BRANCH, base)
    gitenv.with_repo_config(export=EXPORT, rebase_after_squash_merge=True)
    gitenv.github = FakeGitHub()
    gitenv.export_root = gitenv.tmp / "export"
    gitenv.store_factory = lambda repo: LocalDirectorySnapshotStore(gitenv.export_root)
    # A first round of work on design-docs (three commits), then squash-merged as PR #47.
    gitenv.commit_to(BRANCH, {"AGENTS.md": "Agents v2\n"}, "C1")
    gitenv.commit_to(BRANCH, {"AGENTS.md": "Agents v3\n"}, "C2 rewrites C1's line")
    gitenv.commit_to(BRANCH, {"docs/design/RULES.md": "Rules v2\n"}, "C3")
    return gitenv


def assert_exported_state_matches_branches(env: GitEnv, tmp_path):
    snap = (env.export_root / "rot3k-snapshot.zip").read_bytes()
    diff = (env.export_root / "rot3k-working.diff").read_bytes()
    assert reconstruct(tmp_path, snap, diff) == selected_tree(env, env.head(BRANCH))


# ---------------------------------------------------------------- refresh


def test_refresh_rebases_after_squash_merge_without_new_work(env: GitEnv, tmp_path):
    squash = squash_merge(env, 47)
    out = env.repo.refresh()
    assert out.ok, out.error
    r = out.rebase
    assert (r.state, r.pull_request, r.replayed_commits) == ("rebased", 47, 0)
    assert env.head(BRANCH) == squash == r.new_commit  # design-docs now simply equals main
    assert env.head(INTEGRATION) == squash  # main untouched
    assert out.working_diff.empty and (out.working_ahead_by, out.working_behind_by) == (0, 0)
    assert out.message.startswith("Rebased design-docs onto main after PR #47 (0 commit(s) replayed).")
    assert_exported_state_matches_branches(env, tmp_path)


def test_refresh_replays_only_post_merge_commits(env: GitEnv, tmp_path):
    squash = squash_merge(env, 47)
    env.commit_to(BRANCH, {"docs/characters/liu-bei.md": "Liu Bei, round two\n"}, "C4 after merge")
    old_head = env.head(BRANCH)
    out = env.repo.refresh()
    r = out.rebase
    assert (r.state, r.replayed_commits, r.old_commit) == ("rebased", 1, old_head)
    new = env.head(BRANCH)
    assert git(env.origin, "rev-parse", f"{new}^") == squash  # C4' sits directly on main
    assert git(env.origin, "log", "-1", "--format=%an|%cn|%s", new) == "Other Writer|Git Bridge|C4 after merge"
    assert (out.working_ahead_by, out.working_behind_by) == (1, 0)
    diff = (env.export_root / "rot3k-working.diff").read_text()
    assert "+Liu Bei, round two" in diff and "Agents" not in diff  # only the new work
    assert_exported_state_matches_branches(env, tmp_path)


def test_rebased_tree_equals_merge_result(env: GitEnv):
    squash_merge(env, 47)
    env.commit_to(BRANCH, {"docs/design/LORE.md": "Rewritten lore\n"}, "C4")
    before = env.head(BRANCH)
    expected = git(env.origin, "merge-tree", "--write-tree", env.head(INTEGRATION), before).splitlines()[0]
    env.repo.refresh()
    assert git(env.origin, "rev-parse", f"{env.head(BRANCH)}^{{tree}}") == expected


def test_second_refresh_does_not_rebase_again(env: GitEnv):
    squash_merge(env, 47)
    env.repo.refresh()
    head = env.head(BRANCH)
    out = env.repo.refresh()
    assert out.rebase.state == "not_needed" and env.head(BRANCH) == head
    assert not out.message.startswith(("Rebased", "WARNING"))


def test_conflict_changes_nothing_and_warns(env: GitEnv):
    squash_merge(env, 47)
    env.commit_to(BRANCH, {"docs/design/RULES.md": "Rules v3 (design-docs)\n"}, "C4")
    env.commit_to(INTEGRATION, {"docs/design/RULES.md": "Rules v3 (main)\n"}, "other PR on main")
    before = env.head(BRANCH)
    out = env.repo.refresh()
    assert out.ok  # the snapshot is still exported
    assert out.rebase.state == "conflict" and out.rebase.conflicts == ["docs/design/RULES.md"]
    assert env.head(BRANCH) == before
    assert out.message.startswith("WARNING: rebasing design-docs after PR #47 conflicts (docs/design/RULES.md)")
    work = env.settings.work_dir  # caught by merge-tree before any worktree exists
    assert not work.exists() or not any(work.iterdir())


def test_lease_protects_concurrent_push(env: GitEnv, monkeypatch):
    squash_merge(env, 47)
    real_push = Repository._push
    concurrent = {}

    def push_after_someone_else(self, sha, branch, *, lease=None):
        if lease is not None and not concurrent:
            concurrent["sha"] = env.commit_to(BRANCH, {"late.md": "late\n"}, "pushed meanwhile")
        return real_push(self, sha, branch, lease=lease)

    monkeypatch.setattr(Repository, "_push", push_after_someone_else)
    out = env.repo.refresh()
    assert out.rebase.state == "failed" and "nothing was overwritten" in out.rebase.error
    assert env.head(BRANCH) == concurrent["sha"]  # the concurrent commit survives


def test_disabled_rebase_only_warns(env: GitEnv):
    env.with_repo_config(rebase_after_squash_merge=False)
    squash_merge(env, 47)
    before = env.head(BRANCH)
    out = env.repo.refresh()
    assert out.rebase.state == "needed" and env.head(BRANCH) == before
    assert "rebasing is disabled" in out.message


def test_regular_merge_commit_needs_no_rebase(env: GitEnv):
    head = env.head(BRANCH)
    merge = subprocess.run(
        ["git", "commit-tree", git(env.origin, "rev-parse", f"{head}^{{tree}}"), "-p", env.head(INTEGRATION), "-p", head, "-m", "Merge"],
        cwd=env.origin, capture_output=True, env=SQUASH_ENV, check=True,
    ).stdout.decode().strip()
    env.set_branch(INTEGRATION, merge)
    env.github.merged.append({"number": 47, "head": {"sha": head}, "merge_commit_sha": merge, "merged_at": "2026-09-30T10:00:00Z"})
    out = env.repo.refresh()
    assert out.rebase.state == "not_needed" and env.head(BRANCH) == head


def test_no_merged_pr_and_github_failure(env: GitEnv):
    assert env.repo.refresh().rebase.state == "not_needed"
    env.github.fail = True
    out = env.repo.refresh()
    assert out.ok and out.rebase.state == "failed" and "503" in out.rebase.error
    assert out.message.startswith("WARNING: rebase of design-docs failed")


# ------------------------------------------------------------------ push


def test_push_never_rebases_but_warns(env: GitEnv):
    squash_merge(env, 47)
    base, patch = env.make_patch({"docs/design/LORE.md": "More lore\n"})
    out = env.repo.push_and_refresh(env.artifact(patch, base))
    assert out.ok and out.git_push == "success"
    assert git(env.origin, "rev-parse", f"{out.new_sha}^") == base  # on top of the unrebased branch
    assert out.rebase.state == "needed" and out.rebase.pull_request == 47
    assert "WARNING: design-docs still contains squash-merged PR #47; run Refresh Git Snapshot to rebase it." in out.message
    # The next refresh (the Shortcut) rebases, including the pushed commit.
    refreshed = env.repo.refresh()
    assert refreshed.rebase.state == "rebased" and refreshed.rebase.replayed_commits == 1
    assert git(env.origin, "rev-parse", f"{env.head(BRANCH)}^") == env.head(INTEGRATION)


def test_push_without_merged_pr_has_no_warning(env: GitEnv):
    base, patch = env.make_patch({"docs/design/LORE.md": "More lore\n"})
    out = env.repo.push_and_refresh(env.artifact(patch, base))
    assert out.rebase.state == "not_needed" and "WARNING" not in out.message


def test_status_reports_pending_rebase(env: GitEnv):
    squash_merge(env, 47)
    assert env.repo.status().rebase.state == "needed"


def test_lease_push_is_only_allowed_for_working_branch(env: GitEnv):
    repo = env.repo
    repo.ensure_clone()
    with pytest.raises(NotAllowed):
        repo._push(env.head(BRANCH), INTEGRATION, lease=env.head(INTEGRATION))


def test_unexpected_github_client_error_never_breaks_a_push(env: GitEnv):
    class Broken(FakeGitHub):
        def latest_merged_pull_request(self, repo, head, base):
            raise RuntimeError("boom")

    env.github = Broken()
    base, patch = env.make_patch({"docs/design/LORE.md": "More lore\n"})
    out = env.repo.push_and_refresh(env.artifact(patch, base))
    assert out.ok and out.git_push == "success" and out.snapshot_refresh == "success"
    assert out.rebase.state == "failed" and "boom" in out.rebase.error

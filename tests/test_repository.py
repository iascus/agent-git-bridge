from __future__ import annotations

import os
import sys

import pytest

from conftest import BRANCH, GitEnv, git
from git_bridge.config import ValidationCommand
from git_bridge.errors import ConfigError, NotAllowed
from git_bridge.gitcmd import github_auth_env
from git_bridge.repository import Repository

PY = sys.executable


def _assert_no_leftover_worktrees(env: GitEnv) -> None:
    listing = git(env.repo_config.local_path, "worktree", "list", "--porcelain")
    assert listing.count("worktree ") == 1  # only the bare repository itself
    assert not any(env.settings.work_dir.iterdir())


# ---------------------------------------------------------------- happy path


def test_validate_patch_does_not_commit(gitenv: GitEnv):
    base, patch = gitenv.make_patch({"MANIFEST.md": "# Manifest\n\nLine one.\nLine two.\n"})
    outcome = gitenv.repo.validate_patch(gitenv.artifact(patch, base))
    assert outcome.ok, outcome.error
    assert outcome.operation == "validate"
    assert outcome.new_sha is None
    assert outcome.git_publish == "skipped"
    assert outcome.old_sha == base
    assert [(f.path, f.insertions, f.deletions) for f in outcome.changed_files] == [("MANIFEST.md", 1, 0)]
    assert outcome.validation.passed
    assert gitenv.head() == base
    _assert_no_leftover_worktrees(gitenv)


def test_successful_publication(gitenv: GitEnv):
    base, patch = gitenv.make_patch(
        {
            "MANIFEST.md": "# Manifest\n\nLine one, revised.\n",
            "records/new.md": "New record\n",
            "docs/guide.md": None,
        }
    )
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base, commit_message="Refine UI\n\nDetails here.  \n"))
    assert outcome.ok, outcome.error
    assert outcome.git_publish == "success"
    assert outcome.old_sha == base
    assert outcome.new_sha == gitenv.head()
    assert outcome.files_changed == 3
    assert (outcome.insertions, outcome.deletions) == (2, 2)
    assert sorted(f.path for f in outcome.changed_files) == ["MANIFEST.md", "docs/guide.md", "records/new.md"]

    new = outcome.new_sha
    assert git(gitenv.origin, "rev-parse", f"{new}^") == base
    assert git(gitenv.origin, "rev-list", "--count", f"{base}..{new}") == "1"
    assert git(gitenv.origin, "show", "-s", "--format=%an <%ae>", new) == "Git Bridge <git-bridge@localhost>"
    assert git(gitenv.origin, "log", "-1", "--format=%B", new) == "Refine UI\n\nDetails here."
    assert git(gitenv.origin, "show", f"{new}:records/new.md") == "New record"
    # The persistent clone's tracking ref follows the publication.
    assert git(gitenv.repo_config.local_path, "rev-parse", f"refs/remotes/origin/{BRANCH}") == new
    _assert_no_leftover_worktrees(gitenv)


def test_consecutive_publications(gitenv: GitEnv):
    base, patch = gitenv.make_patch({"a.md": "a\n"})
    first = gitenv.repo.publish(gitenv.artifact(patch, base))
    base2, patch2 = gitenv.make_patch({"b.md": "b\n"})
    assert base2 == first.new_sha
    second = gitenv.repo.publish(gitenv.artifact(patch2, base2))
    assert second.ok and second.old_sha == first.new_sha


def test_status_reports_remote_head(gitenv: GitEnv):
    status = gitenv.repo.status()
    assert status.repository == "iascus/rot3k"
    assert [(b.branch, b.remote_sha) for b in status.branches] == [(BRANCH, gitenv.head())]


# --------------------------------------------------------- concurrency safety


def test_wrong_expected_sha_is_409_and_nothing_applied(gitenv: GitEnv):
    base, patch = gitenv.make_patch({"MANIFEST.md": "changed\n"})
    moved = gitenv.advance_remote()
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base))
    assert not outcome.ok
    assert outcome.error.code == "remote_changed"
    assert outcome.error.http_status == 409
    assert outcome.error.details == {"expected": base, "observed": moved}
    assert outcome.git_publish == "failed"
    assert gitenv.head() == moved
    assert outcome.changed_files == []


def test_unknown_expected_sha_is_409(gitenv: GitEnv):
    _, patch = gitenv.make_patch({"MANIFEST.md": "changed\n"})
    outcome = gitenv.repo.publish(gitenv.artifact(patch, "f" * 40))
    assert outcome.error.code == "remote_changed"


def test_concurrent_remote_update_before_push_is_rejected(gitenv: GitEnv, monkeypatch):
    base, patch = gitenv.make_patch({"MANIFEST.md": "mine\n"})
    real_validate = Repository._validate
    competing = {}

    def validate_then_race(self, *args, **kwargs):
        report = real_validate(self, *args, **kwargs)
        competing["sha"] = gitenv.advance_remote()  # another writer wins the race
        return report

    monkeypatch.setattr(Repository, "_validate", validate_then_race)
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base))
    assert not outcome.ok
    assert outcome.error.code == "push_race"
    assert outcome.error.http_status == 409
    assert gitenv.head() == competing["sha"]  # competing history not overwritten
    _assert_no_leftover_worktrees(gitenv)


def test_push_rejected_by_remote_hook(gitenv: GitEnv):
    hook = gitenv.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'protected branch' >&2\nexit 1\n", newline="\n")
    hook.chmod(0o755)
    base, patch = gitenv.make_patch({"MANIFEST.md": "mine\n"})
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base))
    assert not outcome.ok
    assert outcome.error.code == "push_rejected"
    assert outcome.error.http_status == 502
    assert gitenv.head() == base
    _assert_no_leftover_worktrees(gitenv)


# ------------------------------------------------------------- authorisation


def test_disallowed_repository_in_request(gitenv: GitEnv):
    base, patch = gitenv.make_patch({"MANIFEST.md": "x\n"})
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base, repository="iascus/other"))
    assert outcome.error.code == "not_allowed" and outcome.error.http_status == 403
    assert gitenv.head() == base


def test_unknown_repository_key(gitenv: GitEnv):
    with pytest.raises(NotAllowed):
        gitenv.bridge.repository("other")


def test_disallowed_branch(gitenv: GitEnv):
    base, patch = gitenv.make_patch({"MANIFEST.md": "x\n"})
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base, branch="main"))
    assert outcome.error.code == "not_allowed"
    assert git(gitenv.origin, "rev-parse", "refs/heads/main") == base


def test_fetch_refuses_unlisted_branch(gitenv: GitEnv):
    with pytest.raises(NotAllowed):
        gitenv.repo.fetch("main")


def test_existing_clone_with_different_remote_is_refused(gitenv: GitEnv):
    gitenv.repo.ensure_clone()
    gitenv.with_repo_config(remote_url=str(gitenv.tmp / "somewhere-else.git"))
    with pytest.raises(ConfigError):
        gitenv.repo.ensure_clone()


# ------------------------------------------------------------ patch problems


def test_patch_that_does_not_apply(gitenv: GitEnv):
    base, _ = gitenv.make_patch({"MANIFEST.md": "x\n"})
    bogus = (
        b"diff --git a/MANIFEST.md b/MANIFEST.md\n--- a/MANIFEST.md\n+++ b/MANIFEST.md\n"
        b"@@ -1,1 +1,1 @@\n-no such line\n+replacement\n"
    )
    outcome = gitenv.repo.publish(gitenv.artifact(bogus, base))
    assert outcome.error.code == "patch_does_not_apply"
    assert outcome.error.http_status == 422
    assert "MANIFEST.md" in outcome.error.details["stderr"]
    assert gitenv.head() == base
    _assert_no_leftover_worktrees(gitenv)


@pytest.mark.parametrize("target", ["../outside.txt", ".git/hooks/post-checkout", "a/../../outside.txt"])
def test_patch_escaping_tree_is_rejected(gitenv: GitEnv, target):
    base = gitenv.head()
    patch = (
        f"diff --git a/{target} b/{target}\nnew file mode 100644\n--- /dev/null\n+++ b/{target}\n"
        "@@ -0,0 +1 @@\n+owned\n"
    ).encode()
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base))
    assert not outcome.ok
    assert outcome.error.code == "patch_does_not_apply"
    assert not (gitenv.tmp / "outside.txt").exists()
    assert gitenv.head() == base


def test_symlink_patch_rejected(gitenv: GitEnv):
    base = gitenv.head()
    patch = (
        b"diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n"
        b"@@ -0,0 +1 @@\n+/etc/passwd\n\\ No newline at end of file\n"
    )
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base))
    assert outcome.error.code == "patch_policy_violation"
    assert "symlink" in outcome.error.message
    assert gitenv.head() == base


def test_denied_path_rejected(gitenv: GitEnv):
    base, patch = gitenv.make_patch({".github/workflows/ci.yml": "on: push\n"})
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base))
    assert outcome.error.code == "patch_policy_violation"
    assert outcome.error.details["path"] == ".github/workflows/ci.yml"


def test_binary_patch_rejected(gitenv: GitEnv):
    base, patch = gitenv.make_patch({"image.bin": bytes(range(256)) * 4}, binary=True)
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base))
    assert outcome.error.code == "patch_policy_violation"
    assert "binary" in outcome.error.message


def test_patch_without_changes_rejected(gitenv: GitEnv):
    base = gitenv.head()
    empty = b"diff --git a/MANIFEST.md b/MANIFEST.md\n"
    outcome = gitenv.repo.validate_patch(gitenv.artifact(empty, base))
    assert not outcome.ok
    assert outcome.error.code in {"patch_does_not_apply", "patch_policy_violation"}


# ---------------------------------------------------------------- validation


def test_whitespace_errors_fail_builtin_diff_check(gitenv: GitEnv):
    base, patch = gitenv.make_patch({"MANIFEST.md": "# Manifest\n\nLine one.   \n"})
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base))
    assert outcome.error.code == "validation_failed"
    check = outcome.validation.checks[0]
    assert check.name == "git diff --check" and not check.passed
    assert "trailing whitespace" in check.output_tail
    assert gitenv.head() == base


def test_validation_command_failure_leaves_remote_untouched(gitenv: GitEnv):
    gitenv.with_repo_config(
        validation=[ValidationCommand(name="lint", command=[PY, "-c", "import sys; print('bad'); sys.exit(3)"])]
    )
    base, patch = gitenv.make_patch({"MANIFEST.md": "x\n"})
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base))
    assert not outcome.ok
    assert outcome.error.code == "validation_failed"
    assert outcome.error.http_status == 422
    lint = outcome.validation.checks[-1]
    assert (lint.name, lint.passed, lint.exit_code) == ("lint", False, 3)
    assert "bad" in lint.output_tail
    assert gitenv.head() == base
    _assert_no_leftover_worktrees(gitenv)


def test_validation_command_timeout(gitenv: GitEnv):
    gitenv.with_repo_config(
        validation=[ValidationCommand(command=[PY, "-c", "import time; time.sleep(30)"], timeout_seconds=1)]
    )
    base, patch = gitenv.make_patch({"MANIFEST.md": "x\n"})
    outcome = gitenv.repo.validate_patch(gitenv.artifact(patch, base))
    assert outcome.error.code == "validation_failed"
    assert "timed out" in outcome.validation.checks[-1].output_tail


def test_validation_runs_in_patched_worktree_without_credentials(gitenv: GitEnv, monkeypatch):
    token_file = gitenv.tmp / "token"
    token_file.write_text("ghp_SECRET_TOKEN_VALUE")
    gitenv.settings = gitenv.settings.model_copy(update={"github_token_file": token_file})
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SECRET_FROM_ENV")
    script = (
        "import os, sys, pathlib\n"
        "assert pathlib.Path('records/new.md').read_text() == 'hello\\n'\n"
        "leaked = [k for k, v in os.environ.items() if 'SECRET' in v or k.startswith('GIT_CONFIG_KEY')]\n"
        "print(leaked); sys.exit(1 if leaked else 0)\n"
    )
    gitenv.with_repo_config(validation=[ValidationCommand(name="env", command=[PY, "-c", script])])
    base, patch = gitenv.make_patch({"records/new.md": "hello\n"})
    outcome = gitenv.repo.validate_patch(gitenv.artifact(patch, base))
    assert outcome.ok, outcome.validation


def test_commit_contains_exactly_the_patch_even_if_validator_edits_files(gitenv: GitEnv):
    script = (
        "import pathlib, subprocess\n"
        "pathlib.Path('MANIFEST.md').write_text('tampered\\n')\n"
        "pathlib.Path('extra.md').write_text('extra\\n')\n"
        "subprocess.run(['git', 'add', '-A'], check=True)\n"
    )
    gitenv.with_repo_config(validation=[ValidationCommand(command=[PY, "-c", script])])
    base, patch = gitenv.make_patch({"docs/guide.md": "Guide v2\n"})
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base))
    assert outcome.ok, outcome.error
    changed = git(gitenv.origin, "diff", "--name-only", base, outcome.new_sha)
    assert changed == "docs/guide.md"


# ------------------------------------------------------------------ plumbing


def test_github_token_only_travels_in_environment():
    env = github_auth_env("ghp_example")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "ghp_example" not in env["GIT_CONFIG_VALUE_0"]  # base64-encoded, header only
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")


def test_no_network_credentials_for_non_github_remote(gitenv: GitEnv):
    token_file = gitenv.tmp / "token"
    token_file.write_text("ghp_x")
    gitenv.settings = gitenv.settings.model_copy(update={"github_token_file": token_file})
    assert gitenv.repo._network_env() == {}


def test_git_is_not_run_through_a_shell():
    import inspect

    from git_bridge import gitcmd, repository

    for module in (gitcmd, repository):
        source = inspect.getsource(module)
        assert "shell=True," not in source and "shell=True)" not in source
        assert "os.system" not in source
    assert os.devnull in gitcmd.minimal_env()["GIT_CONFIG_GLOBAL"]

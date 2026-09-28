"""Several repositories configured side by side, e.g. rot3k and cyberpunk-tactics."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from conftest import BRANCH, GitEnv, build_gitenv, git
from git_bridge.config import RepositoryConfig, Settings, github_url
from git_bridge.errors import ConfigError
from git_bridge.repository import Bridge


class MultiEnv:
    def __init__(self, tmp_path: Path) -> None:
        self.rot3k = build_gitenv(tmp_path, "rot3k", "iascus/rot3k")
        self.cyber = build_gitenv(tmp_path, "cyberpunk-tactics", "iascus/cyberpunk-tactics")
        self.settings = Settings(
            work_dir=tmp_path / "work",
            repositories={e.key: e.repo_config for e in (self.rot3k, self.cyber)},
        )
        self.bridge = Bridge(self.settings)

    def repo(self, env: GitEnv):
        return self.bridge.repository(env.key)


@pytest.fixture
def multi(tmp_path: Path) -> MultiEnv:
    return MultiEnv(tmp_path)


def test_each_repository_publishes_to_its_own_remote(multi: MultiEnv):
    r_base, r_patch = multi.rot3k.make_patch({"MANIFEST.md": "rot3k change\n"})
    c_base, c_patch = multi.cyber.make_patch({"docs/guide.md": "cyberpunk change\n"})

    r_out = multi.repo(multi.rot3k).publish(multi.rot3k.artifact(r_patch, r_base))
    c_out = multi.repo(multi.cyber).publish(multi.cyber.artifact(c_patch, c_base))

    assert r_out.ok and c_out.ok, (r_out.error, c_out.error)
    assert multi.rot3k.head() == r_out.new_sha
    assert multi.cyber.head() == c_out.new_sha
    assert git(multi.rot3k.origin, "show", f"{r_out.new_sha}:MANIFEST.md") == "rot3k change"
    assert git(multi.cyber.origin, "show", f"{c_out.new_sha}:docs/guide.md") == "cyberpunk change"
    # Separate persistent clones.
    assert multi.rot3k.repo_config.local_path != multi.cyber.repo_config.local_path
    assert not any(multi.settings.work_dir.iterdir())


def test_artifact_for_one_repository_rejected_by_another(multi: MultiEnv):
    base, patch = multi.rot3k.make_patch({"MANIFEST.md": "x\n"})
    cyber_before = multi.cyber.head()
    outcome = multi.repo(multi.cyber).publish(multi.rot3k.artifact(patch, base))
    assert outcome.error.code == "not_allowed"
    assert multi.rot3k.head() == base
    assert multi.cyber.head() == cyber_before


def test_repositories_do_not_share_a_lock(multi: MultiEnv):
    base, patch = multi.cyber.make_patch({"MANIFEST.md": "while rot3k is busy\n"})
    with multi.repo(multi.rot3k).lock:  # rot3k busy with a long operation
        outcome = multi.repo(multi.cyber).publish(multi.cyber.artifact(patch, base))
    assert outcome.ok, outcome.error


def test_parallel_publications_to_different_repositories(multi: MultiEnv):
    jobs = []
    for env in (multi.rot3k, multi.cyber):
        base, patch = env.make_patch({"parallel.md": f"{env.key}\n"})
        jobs.append((env, env.artifact(patch, base)))
    results: dict[str, object] = {}

    def run(env: GitEnv, artifact) -> None:
        results[env.key] = multi.repo(env).publish(artifact)

    threads = [threading.Thread(target=run, args=job) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert all(results[env.key].ok for env, _ in jobs), results
    assert not any(multi.settings.work_dir.iterdir())


def test_parallel_publications_to_same_repository_are_serialised(multi: MultiEnv):
    env = multi.rot3k
    base, patch_a = env.make_patch({"a.md": "a\n"})
    _, patch_b = env.make_patch({"b.md": "b\n"})
    artifacts = [env.artifact(patch_a, base), env.artifact(patch_b, base)]
    results = []
    lock = threading.Lock()

    def run(artifact) -> None:
        out = multi.repo(env).publish(artifact)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=run, args=(a,)) for a in artifacts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    codes = sorted("ok" if r.ok else r.error.code for r in results)
    assert codes == ["ok", "remote_changed"]
    winner = next(r for r in results if r.ok)
    assert env.head() == winner.new_sha
    assert git(env.origin, "rev-list", "--count", f"{base}..{winner.new_sha}") == "1"


def test_status_is_per_repository(multi: MultiEnv):
    for env in (multi.rot3k, multi.cyber):
        status = multi.repo(env).status()
        assert status.key == env.key
        assert status.repository == env.github_repo
        assert status.branches[0].remote_sha == env.head()


def test_stale_worktrees_cleaned_only_for_own_repository(multi: MultiEnv):
    work = multi.settings.work_dir
    work.mkdir(parents=True, exist_ok=True)
    own_leftover = work / "rot3k.abc123"
    other = work / "rot3k-extra.def456"  # a hypothetical key sharing the prefix
    cyber = work / "cyberpunk-tactics.xyz"
    for d in (own_leftover, other, cyber):
        (d / "wt").mkdir(parents=True)
    base, patch = multi.rot3k.make_patch({"a.md": "a\n"})
    assert multi.repo(multi.rot3k).publish(multi.rot3k.artifact(patch, base)).ok
    assert not own_leftover.exists()
    assert other.exists() and cyber.exists()


# ------------------------------------------------------------------ renames


def test_former_name_accepted_in_request(gitenv: GitEnv):
    gitenv.with_repo_config(github_repo="iascus/rot3k", former_github_repos=["iascus/rt3k"])
    base, patch = gitenv.make_patch({"MANIFEST.md": "renamed\n"})
    outcome = gitenv.repo.publish(gitenv.artifact(patch, base, repository="iascus/rt3k"))
    assert outcome.ok, outcome.error


def _bare_clone_pointing_at(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    git(path.parent, "init", "--quiet", "--bare", str(path))
    git(path, "remote", "add", "origin", url)


def test_clone_follows_declared_rename(tmp_path: Path):
    local = tmp_path / "cache" / "rot3k.git"
    _bare_clone_pointing_at(local, github_url("iascus/rt3k"))
    cfg = RepositoryConfig(
        github_repo="iascus/rot3k",
        former_github_repos=["iascus/rt3k"],
        local_path=local,
        allowed_branches=[BRANCH],
    )
    bridge = Bridge(Settings(work_dir=tmp_path / "work", repositories={"rot3k": cfg}))
    bridge.repository("rot3k").ensure_clone()
    assert git(local, "remote", "get-url", "origin") == "https://github.com/iascus/rot3k.git"


def test_clone_with_undeclared_old_url_is_refused(tmp_path: Path):
    local = tmp_path / "cache" / "rot3k.git"
    _bare_clone_pointing_at(local, github_url("iascus/rt3k"))
    cfg = RepositoryConfig(github_repo="iascus/rot3k", local_path=local, allowed_branches=[BRANCH])
    bridge = Bridge(Settings(work_dir=tmp_path / "work", repositories={"rot3k": cfg}))
    with pytest.raises(ConfigError):
        bridge.repository("rot3k").ensure_clone()


# ----------------------------------------------------- configuration sanity


def _repo(name: str, path: str, **extra) -> dict:
    return {"github_repo": name, "local_path": path, "allowed_branches": ["design-docs"], **extra}


@pytest.mark.parametrize(
    "repos,work_dir",
    [
        # same GitHub repository under two keys
        ({"a": _repo("iascus/rot3k", "/srv/a"), "b": _repo("iascus/ROT3K", "/srv/b")}, "/srv/work"),
        # a former name colliding with another repository
        (
            {"a": _repo("iascus/rot3k", "/srv/a", former_github_repos=["iascus/cyberpunk-tactics"]),
             "b": _repo("iascus/cyberpunk-tactics", "/srv/b")},
            "/srv/work",
        ),
        # nested clones
        ({"a": _repo("iascus/rot3k", "/srv/a"), "b": _repo("iascus/cyberpunk-tactics", "/srv/a/b")}, "/srv/work"),
        # clone inside the temporary work directory
        ({"a": _repo("iascus/rot3k", "/srv/work/a")}, "/srv/work"),
        # overlapping Drive export roots
        (
            {"a": _repo("iascus/rot3k", "/srv/a", export={"drive_root": "ChatGPT"}),
             "b": _repo("iascus/cyberpunk-tactics", "/srv/b", export={"drive_root": "ChatGPT/cyberpunk"})},
            "/srv/work",
        ),
    ],
)
def test_repositories_must_be_independent(repos, work_dir):
    with pytest.raises(ValueError):
        Settings.model_validate({"work_dir": work_dir, "repositories": repos})

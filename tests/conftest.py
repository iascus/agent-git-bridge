from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from git_bridge.artifact import PublishArtifact, parse_artifact
from git_bridge.config import Limits, RepositoryConfig, Settings, ValidationCommand
from git_bridge.repository import Bridge, Repository

BRANCH = "design-docs"
GITHUB_REPO = "iascus/rot3k"

_TEST_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Other Writer",
    "GIT_AUTHOR_EMAIL": "other@example.com",
    "GIT_COMMITTER_NAME": "Other Writer",
    "GIT_COMMITTER_EMAIL": "other@example.com",
}


def git(cwd: Path, *args: str, input: bytes | None = None) -> str:
    env = {**os.environ, **_TEST_GIT_ENV}
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        input=input,
        capture_output=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr.decode()}")
    return proc.stdout.decode().strip()


def make_zip(members: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in members.items():
            zf.writestr(zipfile.ZipInfo(name), data, compress_type=compression)
    return buf.getvalue()


def request_for(patch: bytes, base: str, **overrides) -> dict:
    req = {
        "format_version": 1,
        "repository": GITHUB_REPO,
        "branch": BRANCH,
        "expected_base_sha": base,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "commit_message": "Refine encounter activation UI",
    }
    req.update(overrides)
    return req


def artifact_zip(patch: bytes, base: str, **overrides) -> bytes:  
    req = request_for(patch, base, **overrides)
    return make_zip({"request.json": json.dumps(req).encode(), "changes.patch": patch})


@dataclass
class GitEnv:
    """One simulated GitHub remote (bare repo) plus a seed clone for other writers."""

    tmp: Path
    origin: Path
    seed: Path
    repo_config: RepositoryConfig
    settings: Settings
    key: str = "rot3k"
    limits: Limits = field(default_factory=Limits)

    @property
    def github_repo(self) -> str:
        return self.repo_config.github_repo

    @property
    def bridge(self) -> Bridge:
        return Bridge(self.settings)

    @property
    def repo(self) -> Repository:
        return self.bridge.repository(self.key)

    def with_repo_config(self, **changes) -> "GitEnv":
        cfg = self.repo_config.model_copy(update=changes)
        self.repo_config = cfg
        repositories = {**self.settings.repositories, self.key: cfg}
        self.settings = self.settings.model_copy(update={"repositories": repositories})
        return self

    def head(self, branch: str = BRANCH) -> str:
        return git(self.origin, "rev-parse", f"refs/heads/{branch}")

    def _reset_seed(self) -> None:
        git(self.seed, "fetch", "--quiet", "origin")
        git(self.seed, "checkout", "--quiet", "-B", BRANCH, f"origin/{BRANCH}")
        git(self.seed, "reset", "--quiet", "--hard", f"origin/{BRANCH}")
        git(self.seed, "clean", "-fdq")

    def _write(self, edits: dict[str, bytes | str | None]) -> None:
        for rel, content in edits.items():
            path = self.seed / rel
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode() if isinstance(content, str) else content)

    def make_patch(self, edits: dict[str, bytes | str | None], *, binary: bool = False) -> tuple[str, bytes]:
        """Return (base_sha, patch) for edits against the current remote head."""
        self._reset_seed()
        base = git(self.seed, "rev-parse", "HEAD")
        self._write(edits)
        git(self.seed, "add", "-A")
        args = ["diff", "--cached", "--no-color"] + (["--binary"] if binary else [])
        env = {**os.environ, **_TEST_GIT_ENV}
        patch = subprocess.run(["git", *args], cwd=self.seed, capture_output=True, env=env, check=True).stdout
        self._reset_seed()
        return base, patch

    def advance_remote(self, path: str = "concurrent.md", content: str = "someone else\n") -> str:
        self._reset_seed()
        self._write({path: content})
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "--quiet", "-m", "Concurrent change")
        git(self.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
        return self.head()

    def artifact(self, patch: bytes, base: str, **overrides) -> PublishArtifact:
        overrides.setdefault("repository", self.github_repo)
        return parse_artifact(artifact_zip(patch, base, **overrides), self.limits)


def build_gitenv(tmp_path: Path, key: str = "rot3k", github_repo: str = GITHUB_REPO) -> GitEnv:
    root = tmp_path / key
    root.mkdir()
    origin = root / "origin.git"
    git(root, "init", "--quiet", "--bare", str(origin))
    seed = root / "seed"
    git(root, "init", "--quiet", "-b", BRANCH, str(seed))
    (seed / "MANIFEST.md").write_bytes(b"# Manifest\n\nLine one.\n")
    (seed / "docs").mkdir()
    (seed / "docs" / "guide.md").write_bytes(b"Guide\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "Initial")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
    git(seed, "push", "--quiet", "origin", "HEAD:refs/heads/main")

    repo_config = RepositoryConfig(
        github_repo=github_repo,
        local_path=tmp_path / "cache" / f"{key}.git",
        remote_url=str(origin),
        allowed_branches=[BRANCH],
        denied_paths=[".github/**"],
        validation=[],
    )
    settings = Settings(work_dir=tmp_path / "work", repositories={key: repo_config})
    return GitEnv(tmp=tmp_path, origin=origin, seed=seed, repo_config=repo_config, settings=settings, key=key)


@pytest.fixture
def gitenv(tmp_path: Path) -> GitEnv:
    return build_gitenv(tmp_path)


__all__ = ["BRANCH", "GITHUB_REPO", "GitEnv", "build_gitenv", "ValidationCommand", "artifact_zip", "git", "make_zip", "request_for"]

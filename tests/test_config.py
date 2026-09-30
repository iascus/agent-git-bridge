from __future__ import annotations

from pathlib import Path

import pytest

from git_bridge import pathglob
from git_bridge.config import Settings, is_safe_branch_name, load_settings
from git_bridge.errors import ConfigError

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_is_valid():
    settings = load_settings(ROOT / "config" / "example.yaml")
    assert set(settings.repositories) == {"rot3k", "cyberpunk-tactics"}
    repo = settings.repositories["rot3k"]
    assert (repo.integration_branch, repo.working_branch) == ("main", "design-docs")
    assert repo.effective_remote_url == "https://github.com/iascus/rt3k.git"


def test_unknown_keys_rejected(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "work_dir: /tmp/w\nrepositories:\n  r:\n    github_repo: a/b\n    local_path: /tmp/r\n"
        "    integration_branch: main\n    working_branch: dev\n    allow_force_push: true\n"
    )
    with pytest.raises(ConfigError):
        load_settings(cfg)


@pytest.mark.parametrize("bad", ["../x", "-x", "a..b", "a//b", "x.lock", "HEAD", "a b", "x/", "a@{1}", ""])
def test_unsafe_branch_names(bad):
    assert not is_safe_branch_name(bad)


@pytest.mark.parametrize("good", ["main", "design-docs", "feature/x.y", "release_1"])
def test_safe_branch_names(good):
    assert is_safe_branch_name(good)


def test_repositories_must_not_share_local_path():
    repo = {"github_repo": "a/b", "local_path": "/srv/x", "integration_branch": "main", "working_branch": "dev"}
    with pytest.raises(ValueError):
        Settings.model_validate({"work_dir": "/w", "repositories": {"a": repo, "b": repo}})


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("docs/a.md", "docs/**/*.md", True),
        ("docs/x/y/a.md", "docs/**/*.md", True),
        ("docs/a.txt", "docs/**/*.md", False),
        ("MANIFEST.md", "*.md", True),
        ("docs/MANIFEST.md", "*.md", False),
        (".github/workflows/ci.yml", ".github/**", True),
        ("characters/index-a.json", "characters/index*.json", True),
        ("characters/sub/index.json", "characters/index*.json", False),
    ],
)
def test_pathglob(path, pattern, expected):
    assert pathglob.match(path, pattern) is expected

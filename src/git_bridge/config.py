"""Server configuration. Everything the bridge may touch is declared here;
request input is only ever matched against it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ConfigError

GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
REPO_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BRANCH_CHARS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


def is_safe_branch_name(name: str) -> bool:
    """Conservative subset of git-check-ref-format for branch names."""
    if not isinstance(name, str) or not _BRANCH_CHARS_RE.match(name):
        return False
    if ".." in name or "//" in name or "/." in name or "@{" in name:
        return False
    if name.endswith(("/", ".", ".lock")) or name == "HEAD":
        return False
    return True


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ValidationCommand(_Strict):
    name: str | None = None
    command: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]
    timeout_seconds: Annotated[int, Field(gt=0, le=3600)] = 300

    @property
    def display_name(self) -> str:
        return self.name or " ".join(self.command)


class ExportConfig(_Strict):
    drive_root: str
    bootstrap: list[str] = Field(default_factory=list)
    lazy: list[str] = Field(default_factory=list)
    indexes: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class RepositoryConfig(_Strict):
    github_repo: str
    local_path: Path
    remote: str = "origin"
    remote_url: str | None = None
    allowed_branches: Annotated[list[str], Field(min_length=1)]
    denied_paths: list[str] = Field(default_factory=list)
    validation: list[ValidationCommand] = Field(default_factory=list)
    export: ExportConfig | None = None

    @field_validator("github_repo")
    @classmethod
    def _check_repo(cls, v: str) -> str:
        if not GITHUB_REPO_RE.match(v) or v.endswith((".git", ".")):
            raise ValueError(f"invalid github_repo {v!r}; expected owner/name")
        return v

    @field_validator("remote")
    @classmethod
    def _check_remote(cls, v: str) -> str:
        if not REMOTE_NAME_RE.match(v):
            raise ValueError(f"invalid remote name {v!r}")
        return v

    @field_validator("allowed_branches")
    @classmethod
    def _check_branches(cls, v: list[str]) -> list[str]:
        for b in v:
            if not is_safe_branch_name(b):
                raise ValueError(f"invalid branch name {b!r}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate allowed_branches entry")
        return v

    @property
    def effective_remote_url(self) -> str:
        return self.remote_url or f"https://github.com/{self.github_repo}.git"


class GitIdentity(_Strict):
    name: Annotated[str, Field(min_length=1, max_length=200)] = "Git Bridge"
    email: Annotated[str, Field(min_length=3, max_length=200)] = "git-bridge@localhost"


class Limits(_Strict):
    max_archive_bytes: Annotated[int, Field(gt=0)] = 5 * 1024 * 1024
    max_uncompressed_bytes: Annotated[int, Field(gt=0)] = 20 * 1024 * 1024
    max_member_bytes: Annotated[int, Field(gt=0)] = 16 * 1024 * 1024
    max_entries: Annotated[int, Field(gt=0, le=64)] = 8
    max_request_json_bytes: Annotated[int, Field(gt=0)] = 64 * 1024
    max_commit_message_bytes: Annotated[int, Field(gt=0)] = 16 * 1024


class Settings(_Strict):
    work_dir: Path
    github_token_file: Path | None = None
    git_identity: GitIdentity = GitIdentity()
    limits: Limits = Limits()
    git_timeout_seconds: Annotated[int, Field(gt=0, le=3600)] = 300
    repositories: Annotated[dict[str, RepositoryConfig], Field(min_length=1)]

    @field_validator("repositories")
    @classmethod
    def _check_keys(cls, v: dict[str, RepositoryConfig]) -> dict[str, RepositoryConfig]:
        for key in v:
            if not REPO_KEY_RE.match(key):
                raise ValueError(f"invalid repository key {key!r}")
        return v

    @model_validator(mode="after")
    def _unique_paths(self) -> "Settings":
        paths = [r.local_path.resolve() for r in self.repositories.values()]
        if len(set(paths)) != len(paths):
            raise ValueError("repositories must not share a local_path")
        return self


def load_settings(path: str | Path) -> Settings:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read configuration: {exc}") from exc
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc

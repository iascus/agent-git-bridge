"""Server configuration. Everything the bridge may touch is declared here;
request input is only ever matched against it."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Annotated, Literal

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


def github_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _check_github_repo(v: str) -> str:
    if not GITHUB_REPO_RE.match(v) or v.endswith((".git", ".")):
        raise ValueError(f"invalid github_repo {v!r}; expected owner/name")
    return v


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


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
    """What ChatGPT receives per refresh: a complete snapshot of the
    integration branch and the integration -> working tree overlay."""

    drive_root: str
    # Repository path of the source manifest whose PROJECT_SOURCE_FILES block
    # defines the exported file set. Read from each branch's own commit.
    manifest: str
    # Stable artifact names (defaults: <key>-snapshot.zip, <key>-working.diff).
    snapshot_name: str | None = None
    working_diff_name: str | None = None
    max_file_bytes: Annotated[int, Field(gt=0)] = 10 * 1024 * 1024
    max_total_bytes: Annotated[int, Field(gt=0)] = 200 * 1024 * 1024

    @field_validator("drive_root")
    @classmethod
    def _check_root(cls, v: str) -> str:
        parts = v.strip("/").split("/")
        if not v.strip("/") or any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"invalid drive_root {v!r}")
        return "/".join(parts)

    @field_validator("manifest")
    @classmethod
    def _check_manifest(cls, v: str) -> str:
        parts = v.split("/")
        if v.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"invalid export.manifest path {v!r}")
        return v

    @field_validator("snapshot_name")
    @classmethod
    def _check_snapshot_name(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.zip", v):
            raise ValueError(f"snapshot_name {v!r} must be a plain file name ending in .zip")
        return v

    @field_validator("working_diff_name")
    @classmethod
    def _check_diff_name(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}\.(diff|patch)", v):
            raise ValueError(f"working_diff_name {v!r} must be a plain file name ending in .diff or .patch")
        return v


class PullRequestConfig(_Strict):
    """After a push, open a pull request from the working branch into the
    integration branch unless one is open. The bridge never merges, closes or
    edits pull requests."""

    draft: bool = False


class RepositoryConfig(_Strict):
    github_repo: str
    # Previous names after a GitHub rename: still accepted in request.json, and
    # a clone whose remote is the old GitHub URL is re-pointed automatically.
    former_github_repos: list[str] = Field(default_factory=list)
    local_path: Path
    remote: str = "origin"
    remote_url: str | None = None
    # Integration: read only, exported as the complete baseline (e.g. main).
    integration_branch: str
    # Working: the only branch a push may advance (e.g. design-docs).
    working_branch: str
    denied_paths: list[str] = Field(default_factory=list)
    validation: list[ValidationCommand] = Field(default_factory=list)
    export: ExportConfig | None = None
    pull_request: PullRequestConfig | None = None
    # After a pull request working -> integration was squash-merged, a
    # refresh rebases the working branch onto the integration branch (only
    # the commits made after the merge) and updates it with a lease-guarded
    # force push. Needs the "Pull requests: Read" token permission.
    rebase_after_squash_merge: bool = False

    @field_validator("github_repo")
    @classmethod
    def _check_repo(cls, v: str) -> str:
        return _check_github_repo(v)

    @field_validator("former_github_repos")
    @classmethod
    def _check_former(cls, v: list[str]) -> list[str]:
        return [_check_github_repo(r) for r in v]

    @field_validator("remote")
    @classmethod
    def _check_remote(cls, v: str) -> str:
        if not REMOTE_NAME_RE.match(v):
            raise ValueError(f"invalid remote name {v!r}")
        return v

    @field_validator("integration_branch", "working_branch")
    @classmethod
    def _check_branch(cls, v: str) -> str:
        if not is_safe_branch_name(v):
            raise ValueError(f"invalid branch name {v!r}")
        return v

    @model_validator(mode="after")
    def _distinct_roles(self) -> "RepositoryConfig":
        if self.integration_branch == self.working_branch:
            raise ValueError("integration_branch and working_branch must differ")
        return self

    @property
    def branches(self) -> tuple[str, str]:
        return (self.integration_branch, self.working_branch)

    @property
    def effective_remote_url(self) -> str:
        return self.remote_url or github_url(self.github_repo)

    @property
    def accepted_github_repos(self) -> list[str]:
        return [self.github_repo, *self.former_github_repos]

    @property
    def former_remote_urls(self) -> list[str]:
        """Old URLs a clone may be migrated from (only for default GitHub URLs)."""
        if self.remote_url is not None:
            return []
        return [github_url(r) for r in self.former_github_repos]


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


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ServerConfig(_Strict):
    # Loopback only. TLS and tailnet exposure are Tailscale Serve's job.
    host: str = "127.0.0.1"
    port: Annotated[int, Field(gt=0, lt=65536)] = 8000
    bearer_token_file: Path | None = None
    # Records the running service so a restart can stop it first.
    # Default: <work_dir>/../git-bridge-serve.pid
    pid_file: Path | None = None

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if not is_loopback_host(v):
            raise ValueError(
                f"server.host {v!r} is not a loopback address; the bridge must only listen on "
                "127.0.0.1/::1 and be exposed through Tailscale Serve"
            )
        return v


class SnapshotConfig(_Strict):
    transport: Literal["google_drive", "local", "none"] = "none"
    # Target directory for the "local" transport (development / testing).
    local_root: Path | None = None

    @model_validator(mode="after")
    def _local_needs_root(self) -> "SnapshotConfig":
        if self.transport == "local" and self.local_root is None:
            raise ValueError("snapshot.local_root is required for the local transport")
        return self


class GoogleConfig(_Strict):
    client_secrets_file: Path
    token_file: Path


# Extra Git settings an operator may set (e.g. http.sslBackend=schannel on
# Windows). Anything that could run programs or change history is excluded.
ALLOWED_EXTRA_GIT_CONFIG = frozenset({"http.sslbackend", "http.sslcainfo", "http.proxy", "http.version"})


class Settings(_Strict):
    work_dir: Path
    github_token_file: Path | None = None
    git_identity: GitIdentity = GitIdentity()
    git_extra_config: list[str] = Field(default_factory=list)
    limits: Limits = Limits()
    git_timeout_seconds: Annotated[int, Field(gt=0, le=3600)] = 300
    server: ServerConfig = ServerConfig()
    snapshot: SnapshotConfig = SnapshotConfig()
    google: GoogleConfig | None = None
    audit_log: Path | None = None
    repositories: Annotated[dict[str, RepositoryConfig], Field(min_length=1)]

    @field_validator("git_extra_config")
    @classmethod
    def _check_extra_config(cls, v: list[str]) -> list[str]:
        for item in v:
            key, sep, _ = item.partition("=")
            if not sep or key.strip().lower() not in ALLOWED_EXTRA_GIT_CONFIG:
                raise ValueError(f"git_extra_config entry {item!r} is not allowed")
        return v

    @model_validator(mode="after")
    def _google_needs_credentials(self) -> "Settings":
        if self.snapshot.transport == "google_drive" and self.google is None:
            raise ValueError("snapshot.transport google_drive requires a google section")
        return self

    @field_validator("repositories")
    @classmethod
    def _check_keys(cls, v: dict[str, RepositoryConfig]) -> dict[str, RepositoryConfig]:
        for key in v:
            if not REPO_KEY_RE.match(key):
                raise ValueError(f"invalid repository key {key!r}")
        return v

    @model_validator(mode="after")
    def _repositories_are_independent(self) -> "Settings":
        """Each configured repository must own its GitHub name, clone and
        export folder outright, so operations on one can never touch another."""
        work_dir = self.work_dir.resolve()
        seen_names: dict[str, str] = {}
        paths: list[tuple[str, Path]] = []
        drive_roots: list[tuple[str, str]] = []
        for key, repo in self.repositories.items():
            for name in repo.accepted_github_repos:
                other = seen_names.setdefault(name.casefold(), key)
                if other != key or repo.accepted_github_repos.count(name) > 1:
                    raise ValueError(f"GitHub repository {name!r} is configured more than once")
            path = repo.local_path.resolve()
            if _overlaps(path, work_dir):
                raise ValueError(f"local_path of {key!r} overlaps work_dir")
            for other_key, other_path in paths:
                if _overlaps(path, other_path):
                    raise ValueError(f"local_path of {key!r} overlaps that of {other_key!r}")
            paths.append((key, path))
            if repo.export is not None:
                root = repo.export.drive_root.strip("/")
                for other_key, other_root in drive_roots:
                    if root == other_root or root.startswith(other_root + "/") or other_root.startswith(root + "/"):
                        raise ValueError(f"export.drive_root of {key!r} overlaps that of {other_key!r}")
                drive_roots.append((key, root))
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

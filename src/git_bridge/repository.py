"""Per-repository Git operations: clone cache, fetch/status, and the guarded
validate/publish pipeline running in an isolated temporary worktree."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from . import pathglob
from .artifact import PublishArtifact, PublishRequest
from .config import RepositoryConfig, Settings, ValidationCommand
from .errors import (
    BridgeError,
    ConfigError,
    GitError,
    NotAllowed,
    PatchDoesNotApply,
    PatchPolicyViolation,
    PushRace,
    PushRejected,
    RemoteChanged,
    SnapshotError,
    SnapshotNotConfigured,
    UnknownRepository,
    ValidationFailed,
)
from .events import EventLog
from .github import GitHubApi, GitHubClient
from .gitcmd import Git, github_auth_env, minimal_env, tail
from .results import (
    BranchStatus,
    ChangedFile,
    CheckResult,
    ErrorInfo,
    PublishOutcome,
    PullRequestInfo,
    RefreshOutcome,
    RepositoryStatus,
    SnapshotInfo,
    ValidationReport,
)
from .snapshot import build_snapshot, export_archive, export_snapshot
from .store import SnapshotStore

StoreFactory = Callable[["Repository"], SnapshotStore]

REGULAR_FILE_MODES = frozenset({"100644", "100755", "000000"})
_PUSH_RACE_REASONS = ("non-fast-forward", "fetch first", "stale info", "already exists")
VALIDATION_OUTPUT_TAIL = 4000


def _error_info(exc: BridgeError) -> ErrorInfo:
    return ErrorInfo(code=exc.code, message=exc.message, http_status=exc.http_status, details=exc.details)


def normalise_commit_message(message: str) -> str:
    lines = [line.rstrip() for line in message.strip().splitlines()]
    return "\n".join(lines) + "\n"


class Repository:
    def __init__(
        self,
        key: str,
        config: RepositoryConfig,
        settings: Settings,
        *,
        events: EventLog | None = None,
        store_factory: StoreFactory | None = None,
        github: GitHubApi | None = None,
    ) -> None:
        self.key = key
        self.config = config
        self.settings = settings
        self.events = events or EventLog()
        self.store_factory = store_factory
        self.github = github
        self.git = Git(
            config.local_path, timeout=settings.git_timeout_seconds, extra_config=settings.git_extra_config
        )
        # Git operations on this repository's clone are serialised...
        self.lock = threading.Lock()
        # ...and so are snapshot exports, separately, so a slow Drive upload
        # never blocks a publication.
        self.export_lock = threading.Lock()

    # ------------------------------------------------------------------ setup

    def _network_env(self) -> dict[str, str]:
        if self.settings.github_token_file is None or not self.config.effective_remote_url.startswith(
            "https://github.com/"
        ):
            return {}
        return github_auth_env(self.github_token())

    def github_token(self) -> str:
        token_file = self.settings.github_token_file
        if token_file is None:
            raise ConfigError("github_token_file is not configured")
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError("cannot read GitHub token file") from exc
        if not token:
            raise ConfigError("GitHub token file is empty")
        return token

    def ensure_clone(self) -> None:
        """Create the persistent bare clone, or verify an existing one."""
        path = self.config.local_path
        url = self.config.effective_remote_url
        remote = self.config.remote
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.git.at(path.parent).run(["init", "--bare", "--quiet", str(path)])
            self.git.run(["remote", "add", remote, url])
            self.events.emit("clone_initialised", repo=self.key)
            return
        try:
            is_bare = self.git.text(["rev-parse", "--is-bare-repository"])
            actual_url = self.git.text(["remote", "get-url", remote])
        except GitError as exc:
            raise ConfigError(f"local clone for {self.key!r} is not a usable repository") from exc
        if is_bare != "true":
            raise ConfigError(f"local clone for {self.key!r} must be a bare repository")
        if actual_url in self.config.former_remote_urls:
            # Declared GitHub rename: follow it rather than relying on redirects.
            self.git.run(["remote", "set-url", remote, url])
            self.events.emit("clone_remote_renamed", repo=self.key, old=actual_url, new=url)
        elif actual_url != url:
            raise ConfigError(
                f"local clone for {self.key!r} points at a different remote URL; "
                "fix the configuration or delete the clone (it is a disposable cache)"
            )

    def tracking_ref(self, branch: str) -> str:
        return f"refs/remotes/{self.config.remote}/{branch}"

    def fetch(self, branch: str) -> str:
        """Fetch exactly one allowlisted branch; return its remote head SHA."""
        if branch not in self.config.allowed_branches:
            raise NotAllowed("branch is not allowed")
        refspec = f"+refs/heads/{branch}:{self.tracking_ref(branch)}"
        self.git.run(
            ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head", self.config.remote, refspec],
            env=self._network_env(),
        )
        return self.git.text(["rev-parse", "--verify", "--end-of-options", self.tracking_ref(branch) + "^{commit}"])

    def status(self) -> RepositoryStatus:
        with self.lock:
            self.ensure_clone()
            branches = []
            for branch in self.config.allowed_branches:
                try:
                    branches.append(BranchStatus(branch=branch, remote_sha=self.fetch(branch)))
                except GitError as exc:
                    branches.append(BranchStatus(branch=branch, remote_sha=None, error=exc.message))
        status = RepositoryStatus(repository=self.config.github_repo, key=self.key, branches=branches)
        if self.config.export is not None and self.store_factory is not None:
            status.export_branch = self.config.export_branch
            try:
                store = self.store_factory(self)
                if self.config.export.format == "archive":
                    info = store.get_archive_info() or {}
                    files = info.get("files", "")
                    status.snapshot = SnapshotInfo(
                        state="complete" if info else None,
                        commit=info.get("commit"),
                        generation_id=info.get("generation_id"),
                        generated_at=info.get("generated_at"),
                        file_count=int(files) if files.isdigit() else None,
                        archive=info.get("name"),
                    )
                else:
                    current = store.get_snapshot() or {}
                    status.snapshot = SnapshotInfo(
                        state=current.get("state"),
                        commit=current.get("commit") or current.get("target_commit"),
                        generation_id=current.get("generation_id"),
                        generated_at=current.get("generated_at"),
                        file_count=(current.get("counts") or {}).get("exported"),
                    )
            except Exception as exc:  # status must still report Git state
                status.snapshot = SnapshotInfo(error=f"{type(exc).__name__}: {str(exc)[:300]}")
        return status

    # --------------------------------------------------------------- snapshot

    @property
    def archive_name(self) -> str:
        export = self.config.export
        return (export.archive_name if export and export.archive_name else f"{self.key}-snapshot.zip")

    def _store(self) -> SnapshotStore:
        if self.config.export is None:
            raise SnapshotNotConfigured("repository has no export configuration")
        if self.store_factory is None:
            raise SnapshotNotConfigured("no snapshot transport is configured")
        try:
            return self.store_factory(self)
        except BridgeError:
            raise
        except Exception as exc:
            raise SnapshotError(f"cannot open snapshot store: {type(exc).__name__}: {str(exc)[:300]}") from exc

    def refresh(self, *, commit: str | None = None) -> RefreshOutcome:
        """Export the configured branch (or ``commit``, which must be on it)."""
        branch = self.config.export_branch
        outcome = RefreshOutcome(repository=self.config.github_repo, key=self.key, branch=branch)
        started = time.monotonic()
        try:
            store = self._store()
            with self.lock:
                self.ensure_clone()
                head = self.fetch(branch)
                target = commit or head
                if commit is not None:
                    # Only export commits that really are part of the remote branch.
                    reachable = self.git.run(["merge-base", "--is-ancestor", commit, head], check=False)
                    if reachable.returncode != 0:
                        raise SnapshotError("commit is not on the remote branch", commit=commit, head=head)
                build = build_snapshot(
                    self.git,
                    commit=target,
                    repository=self.config.github_repo,
                    repository_key=self.key,
                    branch=branch,
                    export=self.config.export,
                )
            with self.export_lock:
                if self.config.export.format == "archive":
                    result = export_archive(store, build, self.archive_name)
                else:
                    result = export_snapshot(store, build)
            m = result.manifest
            outcome.ok = True
            outcome.commit = target
            outcome.previous_commit = m["previous_commit"]
            outcome.generation_id = m["generation_id"]
            outcome.file_count = m["counts"]["listed"]
            outcome.exported = m["counts"]["exported"]
            outcome.not_exported = m["counts"]["listed"] - m["counts"]["exported"]
            outcome.uploaded, outcome.unchanged, outcome.deleted = result.uploaded, result.unchanged, result.deleted
            outcome.archive_name = result.archive_name
            outcome.archive_bytes = result.archive_bytes
            outcome.archive_sha256 = result.archive_sha256
        except BridgeError as exc:
            outcome.error = _error_info(exc)
        self.events.emit(
            "refresh",
            repo=self.key,
            repository=self.config.github_repo,
            branch=branch,
            commit=outcome.commit,
            generation_id=outcome.generation_id,
            ok=outcome.ok,
            error=outcome.error.code if outcome.error else None,
            error_message=outcome.error.message if outcome.error else None,
            exported=outcome.exported,
            uploaded=outcome.uploaded,
            deleted=outcome.deleted,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return outcome.summarise()

    def publish_and_refresh(self, artifact: PublishArtifact) -> PublishOutcome:
        """Publish, then export the newly published commit. A snapshot
        failure is reported but never turns a successful push into a failure."""
        outcome = self.publish(artifact)
        if not outcome.ok or outcome.new_sha is None:
            return outcome
        outcome.pull_request = self._ensure_pull_request(outcome)
        if self.config.export is None or self.store_factory is None:
            outcome.snapshot_refresh = "not_configured"
            return outcome.summarise()
        refreshed = self.refresh(commit=outcome.new_sha)
        if refreshed.ok:
            outcome.snapshot_refresh = "success"
            outcome.snapshot_commit = refreshed.commit
        else:
            outcome.snapshot_refresh = "failed"
            outcome.snapshot_error = refreshed.error.message if refreshed.error else "unknown error"
        self.events.emit(
            "publish_snapshot",
            repo=self.key,
            new_sha=outcome.new_sha,
            snapshot_refresh=outcome.snapshot_refresh,
            snapshot_error=outcome.snapshot_error,
        )
        return outcome.summarise()

    # --------------------------------------------------------------- pipeline

    def validate_patch(self, artifact: PublishArtifact) -> PublishOutcome:
        return self._execute(artifact, publish=False)

    def publish(self, artifact: PublishArtifact) -> PublishOutcome:
        return self._execute(artifact, publish=True)

    def _authorise(self, req: PublishRequest) -> str:
        accepted = {name.casefold() for name in self.config.accepted_github_repos}
        if req.repository.casefold() not in accepted:
            raise NotAllowed("repository in request.json does not match this endpoint's repository")
        for allowed in self.config.allowed_branches:
            if allowed == req.branch:
                return allowed  # use the configured string from here on
        raise NotAllowed("branch is not in the allowlist for this repository")

    def _execute(self, artifact: PublishArtifact, *, publish: bool) -> PublishOutcome:
        req = artifact.request
        outcome = PublishOutcome(
            operation="publish" if publish else "validate",
            repository=req.repository,
            branch=req.branch,
            expected_base_sha=req.expected_base_sha,
            commit_message=req.commit_message,
        )
        started = time.monotonic()
        try:
            branch = self._authorise(req)
            with self.lock:
                self.ensure_clone()
                self._remove_stale_worktrees()
                observed = self.fetch(branch)
                outcome.observed_sha = observed
                outcome.old_sha = observed
                if observed != req.expected_base_sha:
                    raise RemoteChanged(
                        "remote branch has moved; regenerate the patch against the current head",
                        expected=req.expected_base_sha,
                        observed=observed,
                    )
                with self._worktree(req.expected_base_sha) as wt:
                    wt_git = self.git.at(wt)
                    self._apply(wt_git, artifact.patch)
                    self._check_policy(wt_git, req.expected_base_sha)
                    self._collect_stats(wt_git, req.expected_base_sha, outcome)
                    # Capture the tree now: the commit is exactly the patch,
                    # whatever validation commands do to the worktree/index.
                    tree = wt_git.text(["write-tree"])
                    outcome.validation = self._validate(wt_git, wt, req.expected_base_sha)
                    if not outcome.validation.passed:
                        raise ValidationFailed("validation failed; nothing was committed")
                    if publish:
                        new_sha = self._commit(tree, req.expected_base_sha, req.commit_message)
                        self._push(new_sha, branch)
                        outcome.new_sha = new_sha
                        outcome.git_publish = "success"
                        self._refresh_tracking_ref(branch)
                outcome.ok = True
        except BridgeError as exc:
            outcome.ok = False
            if publish:
                outcome.git_publish = "failed"
            outcome.error = _error_info(exc)
        self.events.emit(
            outcome.operation,
            repo=self.key,
            repository=req.repository,
            branch=req.branch,
            expected_sha=req.expected_base_sha,
            observed_sha=outcome.observed_sha,
            new_sha=outcome.new_sha,
            ok=outcome.ok,
            error=outcome.error.code if outcome.error else None,
            changed_paths=[f.path for f in outcome.changed_files],
            insertions=outcome.insertions,
            deletions=outcome.deletions,
            checks=[(c.name, c.exit_code) for c in outcome.validation.checks] if outcome.validation else None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return outcome.summarise()

    def _ensure_pull_request(self, outcome: PublishOutcome) -> PullRequestInfo | None:
        """Open a PR from the published branch into its base unless one is open.
        Failures are reported; they never undo or fail the push."""
        cfg = self.config.pull_request
        if cfg is None:
            return None
        head = outcome.branch
        if head == cfg.base:
            return PullRequestInfo(state="skipped", base=cfg.base, error="branch is the pull request base")
        if self.github is None:
            return PullRequestInfo(state="failed", base=cfg.base, error="no GitHub API client configured")
        repo = self.config.github_repo
        try:
            existing = self.github.find_open_pull_request(repo, head, cfg.base)
            if existing is not None:
                info = PullRequestInfo(
                    state="existing", number=existing.get("number"), url=existing.get("html_url"), base=cfg.base
                )
            else:
                title = normalise_commit_message(outcome.commit_message or "").splitlines()[0][:200] or f"{head} → {cfg.base}"
                body = (
                    f"Opened automatically by Git Bridge after publishing `{(outcome.new_sha or '')[:12]}` "
                    f"to `{head}`.\n\nLater publications to `{head}` are added to this pull request. "
                    "Git Bridge never merges pull requests."
                )
                created = self.github.create_pull_request(repo, head, cfg.base, title, body, cfg.draft)
                info = PullRequestInfo(
                    state="created", number=created.get("number"), url=created.get("html_url"), base=cfg.base
                )
        except BridgeError as exc:
            info = PullRequestInfo(state="failed", base=cfg.base, error=exc.message)
        self.events.emit(
            "pull_request", repo=self.key, head=head, base=cfg.base, state=info.state, number=info.number, error=info.error
        )
        return info

    def _remove_stale_worktrees(self) -> None:
        """Remove worktrees left by a killed process ("<key>.<random>"; keys
        cannot contain "."). Caller holds self.lock,
        so no live worktree of this repository can exist."""
        work_dir = self.settings.work_dir
        if work_dir.is_dir():
            for leftover in work_dir.glob(f"{self.key}.*"):
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                    self.events.emit("stale_worktree_removed", repo=self.key, name=leftover.name)
        self.git.run(["worktree", "prune"], check=False)

    @contextmanager
    def _worktree(self, sha: str) -> Iterator[Path]:
        work_dir = self.settings.work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        parent = Path(tempfile.mkdtemp(prefix=f"{self.key}.", dir=work_dir))
        path = parent / "wt"
        try:
            self.git.run(["worktree", "add", "--quiet", "--detach", str(path), sha])
            head = self.git.at(path).text(["rev-parse", "HEAD"])
            if head != sha:
                raise GitError("temporary worktree is not at the expected commit")
            yield path
        finally:
            self.git.run(["worktree", "remove", "--force", str(path)], check=False)
            shutil.rmtree(parent, ignore_errors=True)
            self.git.run(["worktree", "prune"], check=False)

    def _apply(self, git: Git, patch: bytes) -> None:
        check = git.run(["apply", "--check", "--index"], input=patch, check=False)
        if check.returncode != 0:
            raise PatchDoesNotApply("git apply --check rejected the patch", stderr=tail(check.stderr))
        applied = git.run(["apply", "--index"], input=patch, check=False)
        if applied.returncode != 0:
            raise PatchDoesNotApply("git apply failed", stderr=tail(applied.stderr))

    def _check_policy(self, git: Git, base: str) -> None:
        raw = git.run(["diff", "--cached", "--raw", "-z", "--no-renames", base]).stdout
        fields = raw.split(b"\0")
        entries: list[tuple[str, str]] = []
        i = 0
        while i < len(fields) - 1:
            meta = fields[i].decode("ascii")
            path_bytes = fields[i + 1]
            i += 2
            try:
                path = path_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise PatchPolicyViolation("patch touches a path that is not valid UTF-8") from None
            new_mode = meta.lstrip(":").split()[1]
            entries.append((new_mode, path))

        if not entries:
            raise PatchPolicyViolation("patch produces no changes")
        for new_mode, path in entries:
            if new_mode not in REGULAR_FILE_MODES:
                kind = {"120000": "symlink", "160000": "submodule"}.get(new_mode, f"mode {new_mode}")
                raise PatchPolicyViolation(f"patch would create a {kind}", path=path)
            if pathglob.match_any(path, self.config.denied_paths):
                raise PatchPolicyViolation("patch touches a denied path", path=path)

    def _collect_stats(self, git: Git, base: str, outcome: PublishOutcome) -> None:
        raw = git.run(["diff", "--cached", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base])
        changed = []
        for record in raw.stdout.split(b"\0"):
            if not record:
                continue
            added, deleted, path = record.decode("utf-8").split("\t", 2)
            if added == "-" or deleted == "-":
                raise PatchPolicyViolation("binary changes are not supported", path=path)
            changed.append(ChangedFile(path=path, insertions=int(added), deletions=int(deleted)))
        outcome.changed_files = changed
        outcome.files_changed = len(changed)
        outcome.insertions = sum(c.insertions for c in changed)
        outcome.deletions = sum(c.deletions for c in changed)

    def _validate(self, git: Git, worktree: Path, base: str) -> ValidationReport:
        checks: list[CheckResult] = []
        started = time.monotonic()
        diff_check = git.run(["diff", "--cached", "--check", "--no-ext-diff", base], check=False)
        checks.append(
            CheckResult(
                name="git diff --check",
                passed=diff_check.returncode == 0,
                exit_code=diff_check.returncode,
                duration_ms=int((time.monotonic() - started) * 1000),
                output_tail=tail(diff_check.stdout.decode("utf-8", "replace") + diff_check.stderr, VALIDATION_OUTPUT_TAIL),
            )
        )
        if checks[-1].passed:
            for command in self.config.validation:
                checks.append(self._run_validation_command(command, worktree))
                if not checks[-1].passed:
                    break
        return ValidationReport(passed=all(c.passed for c in checks), checks=checks)

    def _run_validation_command(self, command: ValidationCommand, worktree: Path) -> CheckResult:
        started = time.monotonic()
        # Scrubbed environment: no GitHub token, no Google credentials.
        env = minimal_env({"HOME": str(worktree.parent)})
        try:
            proc = subprocess.run(
                command.command,
                cwd=worktree,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=command.timeout_seconds,
                shell=False,
                check=False,
            )
            exit_code: int | None = proc.returncode
            output = proc.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            exit_code, output = None, f"timed out after {command.timeout_seconds}s"
        except OSError as exc:
            exit_code, output = None, f"could not start command: {exc.strerror}"
        return CheckResult(
            name=command.display_name,
            passed=exit_code == 0,
            exit_code=exit_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            output_tail=tail(output, VALIDATION_OUTPUT_TAIL),
        )

    def _commit(self, tree: str, parent: str, message: str) -> str:
        identity = self.settings.git_identity
        env = {
            "GIT_AUTHOR_NAME": identity.name,
            "GIT_AUTHOR_EMAIL": identity.email,
            "GIT_COMMITTER_NAME": identity.name,
            "GIT_COMMITTER_EMAIL": identity.email,
        }
        body = normalise_commit_message(message).encode("utf-8")
        return self.git.text(["commit-tree", tree, "-p", parent, "-F", "-"], input=body, env=env)

    def _push(self, sha: str, branch: str) -> None:
        # Plain refspec: no leading '+', no --force, no --force-with-lease.
        # A non-fast-forward is rejected by Git and reported, never retried.
        target = f"refs/heads/{branch}"
        result = self.git.run(
            ["push", "--porcelain", self.config.remote, f"{sha}:{target}"],
            env=self._network_env(),
            check=False,
        )
        status_line = None
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[1].endswith(":" + target):
                status_line = parts
                break
        if result.returncode == 0 and status_line and status_line[0] in (" ", "*"):
            return
        if status_line and status_line[0] == "!":
            summary = status_line[2]
            if any(reason in summary for reason in _PUSH_RACE_REASONS):
                raise PushRace("remote branch changed before push; nothing was overwritten", summary=summary)
            raise PushRejected("remote rejected the push", summary=summary, stderr=tail(result.stderr))
        raise GitError("git push failed", returncode=result.returncode, stderr=tail(result.stderr))

    def _refresh_tracking_ref(self, branch: str) -> None:
        try:
            self.fetch(branch)
        except BridgeError as exc:
            self.events.emit("post_push_fetch_failed", repo=self.key, branch=branch, error=exc.code)


class Bridge:
    """Registry of configured repositories. Unknown keys are rejected."""

    def __init__(
        self,
        settings: Settings,
        *,
        events: EventLog | None = None,
        store_factory: StoreFactory | None = None,
        github: GitHubApi | None = None,
    ) -> None:
        self.settings = settings
        self.events = events or EventLog(settings.audit_log)
        self._repositories = {}
        for key, cfg in settings.repositories.items():
            repo = Repository(key, cfg, settings, events=self.events, store_factory=store_factory)
            # Pull requests only for real GitHub remotes, with the bridge's own token.
            if github is not None:
                repo.github = github
            elif cfg.pull_request is not None and cfg.remote_url is None and settings.github_token_file is not None:
                repo.github = GitHubClient(repo.github_token)
            self._repositories[key] = repo

    @property
    def keys(self) -> list[str]:
        return list(self._repositories)

    def repository(self, key: str) -> Repository:
        try:
            return self._repositories[key]
        except KeyError:
            raise UnknownRepository("unknown repository") from None


def default_store_factory(settings: Settings) -> StoreFactory | None:
    """Store factory for the configured snapshot transport."""
    transport = settings.snapshot.transport
    if transport == "none":
        return None
    if transport == "local":
        from .store import LocalDirectorySnapshotStore

        root = settings.snapshot.local_root

        def local(repo: Repository) -> SnapshotStore:
            return LocalDirectorySnapshotStore(root.joinpath(*repo.config.export.drive_root.split("/")))

        return local

    from .drive import GoogleDriveSnapshotStore, build_drive_api

    token_file = settings.google.token_file

    def google(repo: Repository) -> SnapshotStore:
        return GoogleDriveSnapshotStore(
            build_drive_api(token_file), repo_key=repo.key, drive_root=repo.config.export.drive_root
        )

    return google

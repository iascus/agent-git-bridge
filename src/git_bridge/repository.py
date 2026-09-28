"""Per-repository Git operations: clone cache, fetch/status, and the guarded
validate/publish pipeline running in an isolated temporary worktree."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

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
    ValidationFailed,
)
from .gitcmd import Git, github_auth_env, minimal_env, tail
from .results import (
    BranchStatus,
    ChangedFile,
    CheckResult,
    ErrorInfo,
    PublishOutcome,
    RepositoryStatus,
    ValidationReport,
)

log = logging.getLogger("git_bridge")

REGULAR_FILE_MODES = frozenset({"100644", "100755", "000000"})
_PUSH_RACE_REASONS = ("non-fast-forward", "fetch first", "stale info", "already exists")
VALIDATION_OUTPUT_TAIL = 4000


def log_event(event: str, **fields: object) -> None:
    log.info(json.dumps({"event": event, **fields}, sort_keys=True, default=str))


def normalise_commit_message(message: str) -> str:
    lines = [line.rstrip() for line in message.strip().splitlines()]
    return "\n".join(lines) + "\n"


class Repository:
    def __init__(self, key: str, config: RepositoryConfig, settings: Settings) -> None:
        self.key = key
        self.config = config
        self.settings = settings
        self.git = Git(config.local_path, timeout=settings.git_timeout_seconds)
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ setup

    def _network_env(self) -> dict[str, str]:
        token_file = self.settings.github_token_file
        if token_file is None or not self.config.effective_remote_url.startswith("https://github.com/"):
            return {}
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError("cannot read GitHub token file") from exc
        if not token:
            raise ConfigError("GitHub token file is empty")
        return github_auth_env(token)

    def ensure_clone(self) -> None:
        """Create the persistent bare clone, or verify an existing one."""
        path = self.config.local_path
        url = self.config.effective_remote_url
        remote = self.config.remote
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            Git(path.parent).run(["init", "--bare", "--quiet", str(path)])
            self.git.run(["remote", "add", remote, url])
            log_event("clone_initialised", repo=self.key)
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
            log_event("clone_remote_renamed", repo=self.key, old=actual_url, new=url)
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
        return RepositoryStatus(repository=self.config.github_repo, key=self.key, branches=branches)

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
        )
        started = time.monotonic()
        try:
            branch = self._authorise(req)
            with self.lock:
                self.ensure_clone()
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
                    wt_git = Git(wt, timeout=self.settings.git_timeout_seconds)
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
            outcome.error = ErrorInfo(
                code=exc.code, message=exc.message, http_status=exc.http_status, details=exc.details
            )
        log_event(
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
        return outcome

    @contextmanager
    def _worktree(self, sha: str) -> Iterator[Path]:
        work_dir = self.settings.work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        parent = Path(tempfile.mkdtemp(prefix=f"{self.key}-", dir=work_dir))
        path = parent / "wt"
        try:
            self.git.run(["worktree", "add", "--quiet", "--detach", str(path), sha])
            head = Git(path).text(["rev-parse", "HEAD"])
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
            log_event("post_push_fetch_failed", repo=self.key, branch=branch, error=exc.code)


class Bridge:
    """Registry of configured repositories. Unknown keys are rejected."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._repositories = {key: Repository(key, cfg, settings) for key, cfg in settings.repositories.items()}

    def repository(self, key: str) -> Repository:
        try:
            return self._repositories[key]
        except KeyError:
            raise NotAllowed("unknown repository") from None

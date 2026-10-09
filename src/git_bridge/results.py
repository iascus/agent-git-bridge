"""Compact structured results returned by validate/push/refresh/status."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CheckResult(BaseModel):
    name: str
    passed: bool
    exit_code: int | None
    duration_ms: int
    output_tail: str = ""


class ValidationReport(BaseModel):
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)


class ChangedFile(BaseModel):
    path: str
    insertions: int
    deletions: int
    binary: bool = False


class ErrorInfo(BaseModel):
    code: str
    message: str
    http_status: int
    details: dict[str, Any] = Field(default_factory=dict)


class PullRequestInfo(BaseModel):
    state: Literal["created", "existing", "failed"]
    number: int | None = None
    url: str | None = None
    base: str | None = None
    error: str | None = None


class RebaseInfo(BaseModel):
    """Working branch vs. the latest squash-merged PR into the integration branch.

    state:
      not_needed  no squash-merged PR is still contained in the working branch
      needed      one is (push, status, or rebasing disabled): warn only
      rebased     refresh rebased the working branch onto the integration branch
      conflict    the rebase would conflict; nothing was changed
      failed      detection or rebase failed; nothing was changed
    """

    state: Literal["not_needed", "needed", "rebased", "conflict", "failed"]
    pull_request: int | None = None
    merged_head: str | None = None
    old_commit: str | None = None
    new_commit: str | None = None
    replayed_commits: int | None = None
    conflicts: list[str] = Field(default_factory=list)
    error: str | None = None

    def warning(self, working: str, integration: str, *, on_push: bool = False) -> str:
        pr = f"PR #{self.pull_request}" if self.pull_request else "a merged PR"
        if self.state == "rebased":
            return f"Rebased {working} onto {integration} after {pr} ({self.replayed_commits} commit(s) replayed)."
        if self.state == "needed":
            action = "run Refresh Git Snapshot to rebase it" if on_push else "rebasing is disabled for this repository"
            return f"WARNING: {working} still contains squash-merged {pr}; {action}."
        if self.state == "conflict":
            files = ", ".join(self.conflicts[:5]) + (" …" if len(self.conflicts) > 5 else "")
            return f"WARNING: rebasing {working} after {pr} conflicts ({files}); {working} unchanged, rebase it manually."
        if self.state == "failed":
            return f"WARNING: rebase of {working} failed: {self.error}; {working} unchanged."
        return ""


class PushOutcome(BaseModel):
    ok: bool = False
    operation: Literal["validate", "push"]
    repository: str | None = None
    branch: str | None = None
    expected_base_sha: str | None = None
    commit_message: str | None = Field(default=None, exclude=True)  # internal: PR title
    observed_sha: str | None = None
    old_sha: str | None = None
    new_sha: str | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    validation: ValidationReport | None = None
    git_push: Literal["success", "failed", "skipped"] = "skipped"
    # Pull request working -> integration branch (if configured).
    pull_request: PullRequestInfo | None = None
    # Whether the working branch still needs a rebase after a squash merge
    # (a push never rebases; it only warns).
    rebase: RebaseInfo | None = None
    # Drive refresh after a successful push. A failure here never changes
    # git_push; retry with POST /repos/{repo}/refresh.
    snapshot_refresh: Literal["success", "failed", "skipped", "not_configured"] = "skipped"
    snapshot_generation_id: str | None = None
    snapshot_error: str | None = None
    rebase_base: str | None = Field(default=None, exclude=True)  # internal: integration branch name
    error: ErrorInfo | None = None
    # One human-readable line, e.g. for display in an iOS Shortcut.
    message: str = ""

    def summarise(self) -> "PushOutcome":
        if self.error is not None:
            self.message = f"Rejected ({self.error.code}): {self.error.message}"
        elif self.operation == "validate":
            self.message = (
                f"Patch OK for {self.repository}@{self.branch}: {self.files_changed} file(s), "
                f"+{self.insertions} -{self.deletions}. Nothing was committed."
            )
        else:
            self.message = (
                f"Pushed {(self.new_sha or '')[:12]} to {self.repository}@{self.branch}: "
                f"{self.files_changed} file(s), +{self.insertions} -{self.deletions}."
            )
            pr = self.pull_request
            if pr is not None and pr.state == "created":
                self.message += f" Opened PR #{pr.number} into {pr.base}: {pr.url}"
            elif pr is not None and pr.state == "existing":
                self.message += f" PR #{pr.number} into {pr.base} updated: {pr.url}"
            elif pr is not None and pr.state == "failed":
                self.message += f" PR creation FAILED: {pr.error}"
            if self.snapshot_refresh == "failed":
                self.message += " Drive snapshot refresh FAILED; run Refresh Git Snapshot."
            elif self.snapshot_refresh == "success":
                self.message += " Drive snapshot updated."
            if self.rebase is not None and self.rebase.state != "not_needed":
                self.message += " " + self.rebase.warning(
                    self.branch or "working branch", self.rebase_base or "integration branch", on_push=True
                )
        return self


class BranchHead(BaseModel):
    branch: str
    commit: str | None = None
    error: str | None = None


class SnapshotInfo(BaseModel):
    """What is currently in the snapshot store."""

    generation_id: str | None = None
    generated_at: str | None = None
    integration_commit: str | None = None
    working_commit: str | None = None
    snapshot_name: str | None = None
    working_diff_name: str | None = None
    file_count: int | None = None
    # Both artifacts belong to the same generation.
    consistent: bool | None = None
    error: str | None = None


class RepositoryStatus(BaseModel):
    repository: str
    key: str
    integration: BranchHead
    working: BranchHead
    merge_base_commit: str | None = None
    working_ahead_by: int | None = None
    working_behind_by: int | None = None
    rebase: RebaseInfo | None = None
    snapshot: SnapshotInfo | None = None


class WorkingDiffInfo(BaseModel):
    filename: str
    sha256: str
    bytes: int
    empty: bool
    files_changed: int
    insertions: int
    deletions: int


class RefreshOutcome(BaseModel):
    ok: bool = False
    operation: Literal["refresh"] = "refresh"
    repository: str
    key: str
    integration_branch: str
    working_branch: str
    integration_commit: str | None = None
    working_commit: str | None = None
    merge_base_commit: str | None = None
    working_ahead_by: int | None = None
    working_behind_by: int | None = None
    generation_id: str | None = None
    uploaded: bool = False
    snapshot_name: str | None = None
    snapshot_sha256: str | None = None
    snapshot_bytes: int | None = None
    file_count: int = 0
    not_exported: int = 0
    working_diff: WorkingDiffInfo | None = None
    rebase: RebaseInfo | None = None
    error: ErrorInfo | None = None
    message: str = ""

    def summarise(self) -> "RefreshOutcome":
        prefix = ""
        if self.rebase is not None and self.rebase.state != "not_needed":
            prefix = self.rebase.warning(self.working_branch, self.integration_branch) + " "
        if self.error is not None:
            self.message = f"{prefix}Snapshot refresh failed ({self.error.code}): {self.error.message}"
            return self
        diff = self.working_diff
        overlay = (
            "no working changes"
            if diff is None or diff.empty
            else f"overlay {diff.files_changed} file(s) +{diff.insertions} -{diff.deletions}"
        )
        self.message = prefix + (
            f"{self.repository}: {self.integration_branch} {(self.integration_commit or '')[:12]} "
            f"({self.file_count} file(s)) + {self.working_branch} {(self.working_commit or '')[:12]} "
            f"({overlay}); {'uploaded' if self.uploaded else 'unchanged'}."
        )
        return self

"""Compact structured results returned by validate/publish/status."""

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


class ErrorInfo(BaseModel):
    code: str
    message: str
    http_status: int
    details: dict[str, Any] = Field(default_factory=dict)


class PullRequestInfo(BaseModel):
    state: Literal["created", "existing", "failed", "skipped"]
    number: int | None = None
    url: str | None = None
    base: str | None = None
    error: str | None = None


class PublishOutcome(BaseModel):
    ok: bool = False
    operation: Literal["validate", "publish"]
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
    git_publish: Literal["success", "failed", "skipped"] = "skipped"
    # Drive refresh after a successful publication. A failure here never
    # changes git_publish; retry with POST /repos/{repo}/refresh.
    snapshot_refresh: Literal["success", "failed", "skipped", "not_configured"] = "skipped"
    snapshot_commit: str | None = None
    snapshot_error: str | None = None
    # Pull request from the branch into its configured base (if configured).
    pull_request: PullRequestInfo | None = None
    error: ErrorInfo | None = None
    # One human-readable line, e.g. for display in an iOS Shortcut.
    message: str = ""

    def summarise(self) -> "PublishOutcome":
        if self.error is not None:
            self.message = f"Rejected ({self.error.code}): {self.error.message}"
        elif self.operation == "validate":
            self.message = (
                f"Patch OK for {self.repository}@{self.branch}: {self.files_changed} file(s), "
                f"+{self.insertions} -{self.deletions}. Nothing was committed."
            )
        else:
            self.message = (
                f"Published {(self.new_sha or '')[:12]} to {self.repository}@{self.branch}: "
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
        return self


class BranchStatus(BaseModel):
    branch: str
    remote_sha: str | None
    error: str | None = None


class SnapshotInfo(BaseModel):
    state: str | None = None
    commit: str | None = None
    generation_id: str | None = None
    generated_at: str | None = None
    file_count: int | None = None
    archive: str | None = None
    error: str | None = None


class RepositoryStatus(BaseModel):
    repository: str
    key: str
    branches: list[BranchStatus]
    export_branch: str | None = None
    snapshot: SnapshotInfo | None = None


class RefreshOutcome(BaseModel):
    ok: bool = False
    operation: Literal["refresh"] = "refresh"
    repository: str
    key: str
    branch: str | None = None
    commit: str | None = None
    previous_commit: str | None = None
    generation_id: str | None = None
    file_count: int = 0
    exported: int = 0
    uploaded: int = 0
    unchanged: int = 0
    deleted: int = 0
    not_exported: int = 0
    archive_name: str | None = None
    archive_bytes: int | None = None
    archive_sha256: str | None = None
    error: ErrorInfo | None = None
    message: str = ""

    def summarise(self) -> "RefreshOutcome":
        if self.error is not None:
            self.message = f"Snapshot refresh failed ({self.error.code}): {self.error.message}"
        elif self.archive_name is not None:
            state = "uploaded" if self.uploaded else "unchanged"
            self.message = (
                f"Snapshot of {self.repository}@{self.branch} at {(self.commit or '')[:12]}: "
                f"{self.exported} file(s) in {self.archive_name} ({state})."
            )
        else:
            self.message = (
                f"Snapshot of {self.repository}@{self.branch} at {(self.commit or '')[:12]}: "
                f"{self.exported} file(s) exported ({self.uploaded} updated, {self.unchanged} unchanged, "
                f"{self.deleted} removed)."
            )
        return self

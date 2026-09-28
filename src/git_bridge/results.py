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


class PublishOutcome(BaseModel):
    ok: bool = False
    operation: Literal["validate", "publish"]
    repository: str | None = None
    branch: str | None = None
    expected_base_sha: str | None = None
    observed_sha: str | None = None
    old_sha: str | None = None
    new_sha: str | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    validation: ValidationReport | None = None
    git_publish: Literal["success", "failed", "skipped"] = "skipped"
    error: ErrorInfo | None = None


class BranchStatus(BaseModel):
    branch: str
    remote_sha: str | None
    error: str | None = None


class RepositoryStatus(BaseModel):
    repository: str
    key: str
    branches: list[BranchStatus]

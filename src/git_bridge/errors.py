"""Typed rejections. Each carries a stable machine-readable code and the HTTP
status the API layer should use. Messages must never contain credentials,
patch contents or local filesystem paths."""

from __future__ import annotations

from typing import Any


class BridgeError(Exception):
    code = "bridge_error"
    http_status = 500

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ConfigError(BridgeError):
    code = "config_error"
    http_status = 500


class ArtifactError(BridgeError):
    code = "invalid_artifact"
    http_status = 400


class ArtifactTooLarge(ArtifactError):
    code = "artifact_too_large"
    http_status = 413


class ChecksumMismatch(ArtifactError):
    code = "patch_checksum_mismatch"
    http_status = 400


class NotAllowed(BridgeError):
    code = "not_allowed"
    http_status = 403


class RemoteChanged(BridgeError):
    """expected_base_sha does not equal the current remote branch head."""

    code = "remote_changed"
    http_status = 409


class PatchDoesNotApply(BridgeError):
    code = "patch_does_not_apply"
    http_status = 422


class PatchPolicyViolation(BridgeError):
    code = "patch_policy_violation"
    http_status = 422


class ValidationFailed(BridgeError):
    code = "validation_failed"
    http_status = 422


class PushRace(BridgeError):
    """The remote branch moved between our fetch and our push."""

    code = "push_race"
    http_status = 409


class PushRejected(BridgeError):
    """The remote refused the push for another reason (hook, protection)."""

    code = "push_rejected"
    http_status = 502


class GitError(BridgeError):
    code = "git_error"
    http_status = 500

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message, returncode=returncode, stderr=stderr)
        self.returncode = returncode
        self.stderr = stderr


class UnknownRepository(BridgeError):
    code = "unknown_repository"
    http_status = 404


class Unauthorized(BridgeError):
    code = "unauthorized"
    http_status = 401


class SnapshotError(BridgeError):
    """Building or exporting a snapshot failed. Never undoes a Git push."""

    code = "snapshot_failed"
    http_status = 502


class SnapshotNotConfigured(BridgeError):
    code = "snapshot_not_configured"
    http_status = 409

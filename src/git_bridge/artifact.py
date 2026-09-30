"""Parse and validate the push artifact (``<key>-push.zip``).

The archive is read entirely in memory and never extracted to disk. Only the
exact member names of the declared format version are accepted, so path
traversal names are rejected as unexpected members rather than sanitised.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import zipfile
import zlib
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import GITHUB_REPO_RE, Limits, is_safe_branch_name
from .errors import ArtifactError, ArtifactTooLarge, ChecksumMismatch

REQUEST_MEMBER = "request.json"
PATCH_MEMBER = "changes.patch"
MEMBERS_BY_VERSION = {1: frozenset({REQUEST_MEMBER, PATCH_MEMBER})}

OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


class PushRequest(BaseModel):
    """``request.json``, format version 1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1]
    repository: str
    branch: str
    expected_base_sha: str
    patch_sha256: str
    commit_message: Annotated[str, Field(min_length=1)]
    # Informational only; never used for any decision.
    created_at: Annotated[str, Field(max_length=100)] | None = None
    generator: Annotated[str, Field(max_length=100)] | None = None

    @field_validator("format_version", mode="before")
    @classmethod
    def _strict_int(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("format_version must be an integer")
        return v

    @field_validator("repository")
    @classmethod
    def _repo(cls, v: str) -> str:
        if not GITHUB_REPO_RE.match(v):
            raise ValueError("repository must be owner/name")
        return v

    @field_validator("branch")
    @classmethod
    def _branch(cls, v: str) -> str:
        if not is_safe_branch_name(v):
            raise ValueError("invalid branch name")
        return v

    @field_validator("expected_base_sha")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not OBJECT_ID_RE.match(v):
            raise ValueError("expected_base_sha must be a full lowercase hex commit id")
        return v

    @field_validator("patch_sha256")
    @classmethod
    def _sha256(cls, v: str) -> str:
        v = v.lower()
        if not SHA256_RE.match(v):
            raise ValueError("patch_sha256 must be 64 hex characters")
        return v

    @field_validator("commit_message")
    @classmethod
    def _message(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("commit_message must not contain NUL")
        if not v.strip():
            raise ValueError("commit_message must not be blank")
        return v


@dataclass(frozen=True)
class PushArtifact:
    request: PushRequest
    patch: bytes

    @property
    def patch_sha256(self) -> str:
        return hashlib.sha256(self.patch).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ArtifactError(f"duplicate key {key!r} in request.json")
        out[key] = value
    return out


def _safe_name(name: str) -> str:
    """Printable, bounded rendering of an attacker-supplied member name."""
    return repr(name[:120])


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    try:
        with zf.open(info) as fh:
            data = fh.read(limit + 1)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as exc:
        raise ArtifactError(f"corrupt archive member {_safe_name(info.filename)}: {exc}") from exc
    if len(data) > limit:
        raise ArtifactTooLarge(f"archive member {_safe_name(info.filename)} exceeds {limit} bytes when decompressed")
    return data


def parse_artifact(data: bytes, limits: Limits) -> PushArtifact:
    if len(data) > limits.max_archive_bytes:
        raise ArtifactTooLarge(f"archive exceeds {limits.max_archive_bytes} bytes")
    if not data:
        raise ArtifactError("empty upload")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError) as exc:
        raise ArtifactError("upload is not a valid ZIP archive") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries:
            raise ArtifactError(f"archive has {len(infos)} entries; at most {limits.max_entries} allowed")

        allowed = MEMBERS_BY_VERSION[1]
        by_name: dict[str, zipfile.ZipInfo] = {}
        declared_total = 0
        for info in infos:
            name = info.filename
            if name not in allowed:
                raise ArtifactError(f"unexpected archive member {_safe_name(name)}")
            if name in by_name:
                raise ArtifactError(f"duplicate archive member {_safe_name(name)}")
            if info.flag_bits & 0x1:
                raise ArtifactError("encrypted archive members are not supported")
            if info.compress_type not in _ALLOWED_COMPRESSION:
                raise ArtifactError(f"unsupported compression method for {_safe_name(name)}")
            if info.file_size > limits.max_member_bytes:
                raise ArtifactTooLarge(f"archive member {_safe_name(name)} exceeds {limits.max_member_bytes} bytes")
            declared_total += info.file_size
            by_name[name] = info

        if declared_total > limits.max_uncompressed_bytes:
            raise ArtifactTooLarge(f"archive decompresses to more than {limits.max_uncompressed_bytes} bytes")
        for required in (REQUEST_MEMBER, PATCH_MEMBER):
            if required not in by_name:
                raise ArtifactError(f"archive is missing {required}")

        # Declared sizes can lie; enforce limits on the bytes actually produced.
        raw_request = _read_member(zf, by_name[REQUEST_MEMBER], limits.max_request_json_bytes)
        remaining = limits.max_uncompressed_bytes - len(raw_request)
        patch = _read_member(zf, by_name[PATCH_MEMBER], min(limits.max_member_bytes, remaining))

    try:
        decoded = json.loads(raw_request.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise ArtifactError("request.json is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"request.json is not valid JSON: {exc.msg} at line {exc.lineno}") from exc
    if not isinstance(decoded, dict):
        raise ArtifactError("request.json must contain a JSON object")
    if decoded.get("format_version") not in MEMBERS_BY_VERSION or isinstance(decoded.get("format_version"), bool):
        raise ArtifactError("unsupported or missing format_version", supported=sorted(MEMBERS_BY_VERSION))

    try:
        request = PushRequest.model_validate(decoded)
    except ValidationError as exc:
        problems = [
            {"field": ".".join(str(p) for p in err["loc"]), "problem": err["msg"]} for err in exc.errors()
        ]
        raise ArtifactError("request.json failed validation", problems=problems) from exc

    if len(request.commit_message.encode("utf-8")) > limits.max_commit_message_bytes:
        raise ArtifactError(f"commit_message exceeds {limits.max_commit_message_bytes} bytes")
    if not patch.strip():
        raise ArtifactError("changes.patch is empty")

    actual = hashlib.sha256(patch).hexdigest()
    if not hmac.compare_digest(actual, request.patch_sha256):
        raise ChecksumMismatch("changes.patch does not match patch_sha256", actual=actual)

    return PushArtifact(request=request, patch=patch)

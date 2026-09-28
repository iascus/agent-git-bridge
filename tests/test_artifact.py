from __future__ import annotations

import hashlib
import json
import struct
import zipfile

import pytest

from conftest import artifact_zip, make_zip, request_for
from git_bridge.artifact import parse_artifact
from git_bridge.config import Limits
from git_bridge.errors import ArtifactError, ArtifactTooLarge, ChecksumMismatch

BASE = "0123456789abcdef0123456789abcdef01234567"
PATCH = b"diff --git a/x b/x\n"
LIMITS = Limits()


def _members(req: dict | bytes, patch: bytes = PATCH) -> dict[str, bytes]:
    raw = req if isinstance(req, bytes) else json.dumps(req).encode()
    return {"request.json": raw, "changes.patch": patch}


def test_valid_artifact_parses():
    art = parse_artifact(artifact_zip(PATCH, BASE, created_at="2026-09-28T16:00:00Z", generator="chatgpt"), LIMITS)
    assert art.patch == PATCH
    assert art.request.expected_base_sha == BASE
    assert art.request.branch == "design-docs"


def test_stored_compression_is_accepted():
    data = make_zip(_members(request_for(PATCH, BASE)), compression=zipfile.ZIP_STORED)
    assert parse_artifact(data, LIMITS).patch == PATCH


@pytest.mark.parametrize("data", [b"", b"not a zip at all", b"PK\x03\x04garbage"])
def test_malformed_zip_rejected(data):
    with pytest.raises(ArtifactError):
        parse_artifact(data, LIMITS)


def test_missing_request_json():
    with pytest.raises(ArtifactError, match="missing request.json"):
        parse_artifact(make_zip({"changes.patch": PATCH}), LIMITS)


def test_missing_changes_patch():
    data = make_zip({"request.json": json.dumps(request_for(PATCH, BASE)).encode()})
    with pytest.raises(ArtifactError, match="missing changes.patch"):
        parse_artifact(data, LIMITS)


def test_unexpected_member_rejected():
    members = _members(request_for(PATCH, BASE)) | {"notes.txt": b"hi"}
    with pytest.raises(ArtifactError, match="unexpected archive member"):
        parse_artifact(make_zip(members), LIMITS)


def test_directory_entry_rejected():
    members = _members(request_for(PATCH, BASE)) | {"docs/": b""}
    with pytest.raises(ArtifactError, match="unexpected archive member"):
        parse_artifact(make_zip(members), LIMITS)


@pytest.mark.parametrize(
    "name",
    [
        "../request.json",
        "../../etc/passwd",
        "/request.json",
        "/etc/cron.d/x",
        "sub/../changes.patch",
        "./changes.patch",
        "..\\changes.patch",
        "C:/Windows/changes.patch",
        # zipfile truncates at NUL, yielding a duplicate "request.json".
        pytest.param("request.json\x00.txt", marks=pytest.mark.filterwarnings("ignore:Duplicate name")),
    ],
)
def test_path_traversal_member_names_rejected(name):
    members = {"request.json": json.dumps(request_for(PATCH, BASE)).encode(), name: PATCH}
    with pytest.raises(ArtifactError):
        parse_artifact(make_zip(members), LIMITS)


def test_duplicate_member_rejected():
    req = json.dumps(request_for(PATCH, BASE)).encode()
    import io

    buf = io.BytesIO()
    with pytest.warns(UserWarning), zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("request.json", req)
        zf.writestr("changes.patch", PATCH)
        zf.writestr("changes.patch", b"something else")
    with pytest.raises(ArtifactError, match="duplicate archive member"):
        parse_artifact(buf.getvalue(), LIMITS)


def test_checksum_mismatch_rejected():
    req = request_for(PATCH, BASE, patch_sha256=hashlib.sha256(b"other").hexdigest())
    with pytest.raises(ChecksumMismatch):
        parse_artifact(make_zip(_members(req)), LIMITS)


def test_zip_bomb_declared_size_rejected():
    bomb = b"\0" * (LIMITS.max_member_bytes + 1)
    data = make_zip(_members(request_for(bomb, BASE), bomb))
    assert len(data) < LIMITS.max_archive_bytes  # highly compressible
    with pytest.raises(ArtifactTooLarge):
        parse_artifact(data, LIMITS)


def test_total_decompressed_size_rejected():
    limits = Limits(max_member_bytes=1000, max_uncompressed_bytes=1500)
    patch = b"a" * 900
    req = json.dumps(request_for(patch, BASE)).encode() + b" " * 700
    with pytest.raises(ArtifactTooLarge):
        parse_artifact(make_zip({"request.json": req, "changes.patch": patch}), limits)


def _lie_about_size(data: bytes, name: bytes, fake_size: int) -> bytes:
    """Rewrite the declared uncompressed size in local and central headers."""
    buf = bytearray(data)
    pos = 0
    while (pos := buf.find(b"PK\x03\x04", pos)) != -1:
        name_len = struct.unpack_from("<H", buf, pos + 26)[0]
        if bytes(buf[pos + 30 : pos + 30 + name_len]) == name:
            struct.pack_into("<I", buf, pos + 22, fake_size)
        pos += 4
    pos = 0
    while (pos := buf.find(b"PK\x01\x02", pos)) != -1:
        name_len = struct.unpack_from("<H", buf, pos + 28)[0]
        if bytes(buf[pos + 46 : pos + 46 + name_len]) == name:
            struct.pack_into("<I", buf, pos + 24, fake_size)
        pos += 4
    return bytes(buf)


def test_zip_bomb_with_lying_header_rejected():
    limits = Limits(max_member_bytes=10_000, max_uncompressed_bytes=20_000)
    bomb = b"\0" * 500_000
    data = _lie_about_size(make_zip(_members(request_for(bomb, BASE), bomb)), b"changes.patch", 10)
    with pytest.raises(ArtifactError):  # too large, or CRC/size mismatch
        parse_artifact(data, limits)


def test_too_many_entries_rejected():
    members = {f"f{i}": b"" for i in range(LIMITS.max_entries + 1)}
    with pytest.raises(ArtifactError, match="entries"):
        parse_artifact(make_zip(members), LIMITS)


def test_oversized_upload_rejected():
    with pytest.raises(ArtifactTooLarge):
        parse_artifact(b"\0" * (LIMITS.max_archive_bytes + 1), LIMITS)


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[1, 2]", b"\xff\xfe{}", b'{"format_version": 1, "format_version": 1}'],
)
def test_bad_request_json_rejected(raw):
    with pytest.raises(ArtifactError):
        parse_artifact(make_zip(_members(raw)), LIMITS)


@pytest.mark.parametrize(
    "override",
    [
        {"format_version": 2},
        {"format_version": "1"},
        {"format_version": True},
        {"expected_base_sha": "0123456"},
        {"expected_base_sha": BASE.upper()},
        {"expected_base_sha": "HEAD"},
        {"branch": "../main"},
        {"branch": "--force"},
        {"branch": "a..b"},
        {"repository": "https://evil.example/x.git"},
        {"commit_message": "   "},
        {"commit_message": "bad\x00message"},
        {"unexpected_field": "x"},
    ],
)
def test_invalid_request_fields_rejected(override):
    req = request_for(PATCH, BASE, **override)
    with pytest.raises(ArtifactError):
        parse_artifact(make_zip(_members(req)), LIMITS)


def test_missing_required_field_rejected():
    req = request_for(PATCH, BASE)
    del req["expected_base_sha"]
    with pytest.raises(ArtifactError) as exc:
        parse_artifact(make_zip(_members(req)), LIMITS)
    assert any(p["field"] == "expected_base_sha" for p in exc.value.details["problems"])


def test_empty_patch_rejected():
    with pytest.raises(ArtifactError, match="empty"):
        parse_artifact(make_zip(_members(request_for(b"\n", BASE), b"\n")), LIMITS)

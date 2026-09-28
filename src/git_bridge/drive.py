"""Google Drive snapshot transport.

Layout: ``<drive_root>/<repository path>`` as real Drive folders, plus
``<drive_root>/snapshot.json``. Scope ``drive.file`` means the bridge only
sees files and folders it created itself.

Every file the bridge creates carries private ``appProperties``: the
repository key, its kind, the Git blob SHA, and its repository path (split
into chunks because each property is limited to 124 bytes). That makes the
Drive state self-describing: no local index can drift out of sync.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .errors import ConfigError, SnapshotError
from .store import SNAPSHOT_NAME, FileMetadata, SnapshotStore, StoredFile, check_relative_path

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"

_PATH_CHUNK_BYTES = 100  # key "gb_pNN" + value must stay within 124 bytes
_MAX_PATH_CHUNKS = 24  # Drive allows 30 properties per app per file


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    app_properties: dict[str, str]


class DriveApi(Protocol):
    def find_folder(self, name: str, parent_id: str) -> str | None: ...
    def create_folder(self, name: str, parent_id: str, props: dict[str, str]) -> str: ...
    def list_files(self, props: dict[str, str]) -> list[DriveFile]: ...
    def create_file(self, name: str, parent_id: str, content: bytes, mime_type: str, props: dict[str, str]) -> str: ...
    def update_file(
        self, file_id: str, content: bytes, mime_type: str, props: dict[str, str], name: str | None = None
    ) -> None: ...
    def trash_file(self, file_id: str) -> None: ...
    def download(self, file_id: str) -> bytes: ...


# ------------------------------------------------------------ path encoding


def encode_path(path: str) -> dict[str, str]:
    chunks: list[str] = []
    current = ""
    for ch in path:
        if len((current + ch).encode("utf-8")) > _PATH_CHUNK_BYTES:
            chunks.append(current)
            current = ""
        current += ch
    chunks.append(current)
    if len(chunks) > _MAX_PATH_CHUNKS:
        raise SnapshotError(f"path too long for Drive metadata: {path[:120]!r}…")
    props = {f"gb_p{i}": chunk for i, chunk in enumerate(chunks)}
    props["gb_pn"] = str(len(chunks))
    return props


def decode_path(props: dict[str, str]) -> str | None:
    try:
        count = int(props["gb_pn"])
        return "".join(props[f"gb_p{i}"] for i in range(count))
    except (KeyError, ValueError):
        return None


# -------------------------------------------------------------------- store


class GoogleDriveSnapshotStore(SnapshotStore):
    def __init__(self, api: DriveApi, *, repo_key: str, drive_root: str) -> None:
        self.api = api
        self.repo_key = repo_key
        self.drive_root = drive_root.strip("/")
        self._folders: dict[str, str] = {}
        self._files: dict[str, StoredFile] | None = None
        self._snapshot_id: str | None = None

    def _props(self, kind: str, **extra: str) -> dict[str, str]:
        return {"gb_repo": self.repo_key, "gb_kind": kind, **extra}

    def _folder(self, rel_dir: str) -> str:
        """Folder ID for ``drive_root/rel_dir``, creating the chain as needed."""
        full = "/".join(p for p in (self.drive_root, rel_dir) if p)
        if full in self._folders:
            return self._folders[full]
        parent_id, walked = "root", []
        for part in full.split("/"):
            walked.append(part)
            key = "/".join(walked)
            folder_id = self._folders.get(key)
            if folder_id is None:
                folder_id = self.api.find_folder(part, parent_id)
                if folder_id is None:
                    folder_id = self.api.create_folder(part, parent_id, self._props("folder"))
                self._folders[key] = folder_id
            parent_id = folder_id
        return parent_id

    def existing_files(self) -> dict[str, StoredFile]:
        found: dict[str, StoredFile] = {}
        for f in self.api.list_files(self._props("file")):
            path = decode_path(f.app_properties)
            if path is None:
                continue
            if path in found:  # duplicate left by an interrupted run: keep one
                self.api.trash_file(f.id)
                continue
            found[path] = StoredFile(path, f.id, f.app_properties.get("gb_blob"))
        self._files = dict(found)
        return found

    def put_file(self, path: str, content: bytes, metadata: FileMetadata) -> StoredFile:
        rel = check_relative_path(path)
        if self._files is None:
            self.existing_files()
        props = self._props("file", gb_blob=metadata.blob_sha, gb_class=metadata.file_class, **encode_path(path))
        current = self._files.get(path)
        if current is not None:
            # In place: same file ID, name and folder; only content changes.
            self.api.update_file(current.file_id, content, metadata.mime_type, props)
            file_id = current.file_id
        else:
            parent = self._folder(str(rel.parent) if str(rel.parent) != "." else "")
            file_id = self.api.create_file(rel.name, parent, content, metadata.mime_type, props)
        stored = StoredFile(path, file_id, metadata.blob_sha)
        self._files[path] = stored
        return stored

    def delete_file(self, path: str) -> None:
        if self._files is None:
            self.existing_files()
        current = self._files.pop(path, None)
        if current is not None:
            self.api.trash_file(current.file_id)  # recoverable from Drive trash

    def _find_snapshot(self) -> str | None:
        if self._snapshot_id is None:
            matches = self.api.list_files(self._props("snapshot"))
            self._snapshot_id = matches[0].id if matches else None
        return self._snapshot_id

    def put_snapshot(self, snapshot: dict[str, Any]) -> None:
        data = json.dumps(snapshot, indent=2).encode("utf-8") + b"\n"
        props = self._props("snapshot", gb_state=str(snapshot.get("state")))
        snapshot_id = self._find_snapshot()
        if snapshot_id is not None:
            self.api.update_file(snapshot_id, data, "application/json", props)
        else:
            self._snapshot_id = self.api.create_file(SNAPSHOT_NAME, self._folder(""), data, "application/json", props)

    def get_snapshot(self) -> dict[str, Any] | None:
        snapshot_id = self._find_snapshot()
        if snapshot_id is None:
            return None
        return json.loads(self.api.download(snapshot_id).decode("utf-8"))

    # Archive format ------------------------------------------------------

    def _find_archive(self) -> DriveFile | None:
        matches = self.api.list_files(self._props("archive"))
        for extra in matches[1:]:  # left by an interrupted run
            self.api.trash_file(extra.id)
        return matches[0] if matches else None

    def put_archive(self, name: str, data: bytes, info: dict[str, str]) -> str:
        props = self._props("archive", **{f"gb_{k}": v for k, v in info.items()})
        current = self._find_archive()
        if current is not None:
            # Same Drive file: stable ID and name, content replaced in one write.
            self.api.update_file(current.id, data, "application/zip", props, name=name)
            return current.id
        return self.api.create_file(name, self._folder(""), data, "application/zip", props)

    def get_archive_info(self) -> dict[str, str] | None:
        current = self._find_archive()
        if current is None:
            return None
        info = {k[3:]: v for k, v in current.app_properties.items() if k.startswith("gb_")}
        info["name"] = current.name
        return info

    def remove_file_exports(self) -> int:
        removed = 0
        for f in self.api.list_files(self._props("file")):
            self.api.trash_file(f.id)
            removed += 1
        for f in self.api.list_files(self._props("snapshot")):
            self.api.trash_file(f.id)
        self._snapshot_id = None
        self._files = {}
        # Sub-folders created for per-file exports; keep the drive_root chain.
        self._folder("")
        keep = {fid for path, fid in self._folders.items() if (self.drive_root + "/").startswith(path + "/")}
        for f in self.api.list_files(self._props("folder")):
            if f.id not in keep:
                self.api.trash_file(f.id)
        self._folders = {k: v for k, v in self._folders.items() if v in keep}
        return removed


# ------------------------------------------------------- real Drive client


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveApi:
    """Thin wrapper over the Drive v3 API client."""

    RETRIES = 3

    def __init__(self, service: Any) -> None:
        self.files = service.files()

    def _media(self, content: bytes, mime_type: str):
        from googleapiclient.http import MediaIoBaseUpload

        # Resumable uploads for large archives survive transient failures.
        resumable = len(content) > 5 * 1024 * 1024
        return MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=resumable)

    def find_folder(self, name: str, parent_id: str) -> str | None:
        q = (
            f"name = '{_q(name)}' and '{_q(parent_id)}' in parents and "
            f"mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        res = self.files.list(q=q, spaces="drive", fields="files(id)", pageSize=10).execute(num_retries=self.RETRIES)
        found = res.get("files", [])
        return found[0]["id"] if found else None

    def create_folder(self, name: str, parent_id: str, props: dict[str, str]) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id], "appProperties": props}
        return self.files.create(body=body, fields="id").execute(num_retries=self.RETRIES)["id"]

    def list_files(self, props: dict[str, str]) -> list[DriveFile]:
        clauses = [f"appProperties has {{ key='{_q(k)}' and value='{_q(v)}' }}" for k, v in props.items()]
        q = " and ".join(clauses + ["trashed = false"])
        out: list[DriveFile] = []
        token = None
        while True:
            res = self.files.list(
                q=q,
                spaces="drive",
                fields="nextPageToken, files(id, name, appProperties)",
                pageSize=1000,
                pageToken=token,
            ).execute(num_retries=self.RETRIES)
            for f in res.get("files", []):
                out.append(DriveFile(f["id"], f.get("name", ""), f.get("appProperties", {}) or {}))
            token = res.get("nextPageToken")
            if not token:
                return out

    def create_file(self, name: str, parent_id: str, content: bytes, mime_type: str, props: dict[str, str]) -> str:
        body = {"name": name, "parents": [parent_id], "mimeType": mime_type, "appProperties": props}
        req = self.files.create(body=body, media_body=self._media(content, mime_type), fields="id")
        return req.execute(num_retries=self.RETRIES)["id"]

    def update_file(
        self, file_id: str, content: bytes, mime_type: str, props: dict[str, str], name: str | None = None
    ) -> None:
        body: dict[str, Any] = {"appProperties": props}
        if name is not None:
            body["name"] = name
        req = self.files.update(fileId=file_id, body=body, media_body=self._media(content, mime_type), fields="id")
        req.execute(num_retries=self.RETRIES)

    def trash_file(self, file_id: str) -> None:
        self.files.update(fileId=file_id, body={"trashed": True}, fields="id").execute(num_retries=self.RETRIES)

    def download(self, file_id: str) -> bytes:
        return self.files.get_media(fileId=file_id).execute(num_retries=self.RETRIES)


# ------------------------------------------------------------------ OAuth


def _save_token(token_file: Path, data: str) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = token_file.with_suffix(token_file.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, token_file)


def google_login(client_secrets_file: Path, token_file: Path) -> None:
    """Interactive one-time consent in a browser; stores a refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), SCOPES)
    creds = flow.run_local_server(
        host="localhost", port=0, open_browser=True, access_type="offline", prompt="consent"
    )
    _save_token(token_file, creds.to_json())


def load_credentials(token_file: Path):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_file.exists():
        raise ConfigError("no Google token; run `git-bridge google-login` once")
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds.valid:
        return creds
    if not creds.refresh_token:
        raise ConfigError("Google token has no refresh token; run `git-bridge google-login` again")
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise SnapshotError(
            "Google login expired or was revoked; run `git-bridge google-login` again"
        ) from exc
    _save_token(token_file, creds.to_json())
    return creds


def build_drive_api(token_file: Path) -> GoogleDriveApi:
    from googleapiclient.discovery import build

    service = build("drive", "v3", credentials=load_credentials(token_file), cache_discovery=False)
    return GoogleDriveApi(service)

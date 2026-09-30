"""Google Drive snapshot transport.

Per repository, under ``export.drive_root``:

    <key>-snapshot.zip    complete integration-branch snapshot
    <key>-working.diff    integration -> working tree overlay

Each is one Drive file updated in place (stable file ID and name). Scope
``drive.file`` means the bridge only sees files and folders it created.
Private ``appProperties`` record the repository key, the artifact role and
the generation metadata, so the Drive state is self-describing.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .errors import ConfigError, SnapshotError
from .store import Role, SnapshotStore

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"


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


# -------------------------------------------------------------------- store

# Drive "kind" per role. "archive" is kept for the snapshot so the ZIP created
# by earlier versions keeps its Drive file ID.
_KIND = {"snapshot": "archive", "working_diff": "working_diff"}


class GoogleDriveSnapshotStore(SnapshotStore):
    def __init__(self, api: DriveApi, *, repo_key: str, drive_root: str) -> None:
        self.api = api
        self.repo_key = repo_key
        self.drive_root = drive_root.strip("/")
        self._root_id: str | None = None

    def _props(self, kind: str, **extra: str) -> dict[str, str]:
        return {"gb_repo": self.repo_key, "gb_kind": kind, **extra}

    def _root(self) -> str:
        """Folder ID of drive_root, creating the chain of folders as needed."""
        if self._root_id is None:
            parent_id = "root"
            for part in self.drive_root.split("/"):
                folder_id = self.api.find_folder(part, parent_id)
                if folder_id is None:
                    folder_id = self.api.create_folder(part, parent_id, self._props("folder"))
                parent_id = folder_id
            self._root_id = parent_id
        return self._root_id

    def _find(self, role: Role) -> DriveFile | None:
        matches = self.api.list_files(self._props(_KIND[role]))
        for extra in matches[1:]:  # left by an interrupted run
            self.api.trash_file(extra.id)
        return matches[0] if matches else None

    def put_artifact(self, role: Role, name: str, data: bytes, mime_type: str, info: dict[str, str]) -> str:
        props = self._props(_KIND[role], **{f"gb_{k}": v for k, v in info.items()})
        current = self._find(role)
        if current is not None:
            # Same Drive file: stable ID and name, content replaced in one write.
            self.api.update_file(current.id, data, mime_type, props, name=name)
            return current.id
        return self.api.create_file(name, self._root(), data, mime_type, props)

    def get_artifact_info(self, role: Role) -> dict[str, str] | None:
        current = self._find(role)
        if current is None:
            return None
        info = {k[3:]: v for k, v in current.app_properties.items() if k.startswith("gb_")}
        info["name"] = current.name
        return info


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

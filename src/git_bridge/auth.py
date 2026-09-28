"""Application-layer authentication.

Tailscale is the network boundary; this is the independent second check.
``Authenticator`` is the seam for replacing the bearer token later (for
example with the Tailscale identity headers that Serve adds).
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from .errors import ConfigError, Unauthorized

MIN_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class Principal:
    name: str


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> Principal: ...


class BearerTokenAuthenticator:
    def __init__(self, token: str) -> None:
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"bearer token must be at least {MIN_TOKEN_LENGTH} characters")
        self._token = token.encode("utf-8")

    @classmethod
    def from_file(cls, path: Path | None) -> "BearerTokenAuthenticator":
        if path is None:
            raise ConfigError("server.bearer_token_file is not configured; refusing to start without authentication")
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError("cannot read bearer token file") from exc
        return cls(token)

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        header = headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not supplied:
            raise Unauthorized("missing bearer token")
        # Constant-time comparison; also compares lengths without early exit.
        if not hmac.compare_digest(supplied.strip().encode("utf-8"), self._token):
            raise Unauthorized("invalid bearer token")
        return Principal("bearer-token")


def generate_token() -> str:
    return secrets.token_urlsafe(48)


def write_token_file(path: Path, *, overwrite: bool = False) -> None:
    import os

    if path.exists() and not overwrite:
        raise ConfigError(f"{path} already exists; use --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(generate_token() + "\n")

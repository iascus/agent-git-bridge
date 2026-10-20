from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status


def check_bearer_token(authorization: str | None, expected_token: str) -> None:
    """Validate an Authorization: Bearer token using constant-time comparison."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, supplied_token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not secrets.compare_digest(supplied_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_import_token(expected_token: str):
    def dependency(authorization: str | None = Header(default=None)) -> None:
        check_bearer_token(authorization, expected_token)

    return dependency

"""HTTP API. Plain HTTP on loopback only; Tailscale Serve provides HTTPS.

Endpoints:
  GET  /health                          (unauthenticated, reveals nothing)
  GET  /repos/{repo}/status
  POST /repos/{repo}/refresh
  POST /repos/{repo}/validate-patch     body: publish.zip (raw or multipart)
  POST /repos/{repo}/publish            body: publish.zip (raw or multipart)
"""

from __future__ import annotations

import logging
from email import policy
from email.parser import BytesParser

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .artifact import parse_artifact
from .auth import Authenticator, BearerTokenAuthenticator, Principal
from .config import Settings
from .errors import ArtifactError, ArtifactTooLarge, BridgeError, Unauthorized
from .repository import Bridge, Repository, default_store_factory
from .results import ErrorInfo, PublishOutcome

log = logging.getLogger("git_bridge.api")

_UPLOAD_FIELD_NAMES = ("file", "zip", "artifact", "publish")


def _error_response(exc: BridgeError, **extra) -> JSONResponse:
    info = ErrorInfo(code=exc.code, message=exc.message, http_status=exc.http_status, details=exc.details)
    headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, Unauthorized) else None
    body = {"ok": False, **extra, "error": info.model_dump(), "message": f"Rejected ({exc.code}): {exc.message}"}
    return JSONResponse(body, status_code=exc.http_status, headers=headers)


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ArtifactTooLarge(f"upload exceeds {limit} bytes")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ArtifactTooLarge(f"upload exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _extract_upload(content_type: str, body: bytes) -> bytes:
    """Raw ZIP body, or the single file part of a multipart/form-data body."""
    if not content_type.lower().startswith("multipart/"):
        return body
    message = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise ArtifactError("malformed multipart body")
    uploads = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if part.get_filename() or name in _UPLOAD_FIELD_NAMES:
            uploads.append(part.get_payload(decode=True) or b"")
    if len(uploads) != 1:
        raise ArtifactError("multipart body must contain exactly one file")
    return uploads[0]


def create_app(
    settings: Settings,
    *,
    bridge: Bridge | None = None,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    bridge = bridge or Bridge(settings, store_factory=default_store_factory(settings))
    auth = authenticator or BearerTokenAuthenticator.from_file(settings.server.bearer_token_file)
    limits = settings.limits

    # No interactive docs or schema: nothing to browse for anyone on the tailnet.
    app = FastAPI(title="agent-git-bridge", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bridge = bridge

    @app.exception_handler(BridgeError)
    async def _bridge_error(request: Request, exc: BridgeError) -> JSONResponse:
        if isinstance(exc, Unauthorized):
            client = request.client.host if request.client else None
            bridge.events.emit("auth_failed", path=request.url.path, client=client, reason=exc.message)
        return _error_response(exc)

    def require_auth(request: Request) -> Principal:
        return auth.authenticate(request.headers)

    def get_repo(repo: str, _: Principal = Depends(require_auth)) -> Repository:
        return bridge.repository(repo)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/repos/{repo}/status")
    def status(repository: Repository = Depends(get_repo)) -> JSONResponse:
        return JSONResponse(repository.status().model_dump())

    @app.post("/repos/{repo}/refresh")
    def refresh(repository: Repository = Depends(get_repo)) -> JSONResponse:
        outcome = repository.refresh()
        code = outcome.error.http_status if outcome.error else 200
        return JSONResponse(outcome.model_dump(), status_code=code)

    async def _patch_endpoint(request: Request, repository: Repository, operation: str) -> JSONResponse:
        try:
            body = await _read_body(request, limits.max_archive_bytes)
            artifact = parse_artifact(_extract_upload(request.headers.get("content-type", ""), body), limits)
        except BridgeError as exc:
            outcome = PublishOutcome(operation=operation, error=ErrorInfo(
                code=exc.code, message=exc.message, http_status=exc.http_status, details=exc.details
            ))
            if operation == "publish":
                outcome.git_publish = "failed"
            bridge.events.emit(operation, repo=repository.key, ok=False, error=exc.code)
            return JSONResponse(outcome.summarise().model_dump(), status_code=exc.http_status)
        if operation == "publish":
            outcome = await run_in_threadpool(repository.publish_and_refresh, artifact)
        else:
            outcome = await run_in_threadpool(repository.validate_patch, artifact)
        code = outcome.error.http_status if outcome.error else 200
        return JSONResponse(outcome.model_dump(), status_code=code)

    @app.post("/repos/{repo}/validate-patch")
    async def validate_patch(request: Request, repository: Repository = Depends(get_repo)) -> JSONResponse:
        return await _patch_endpoint(request, repository, "validate")

    @app.post("/repos/{repo}/publish")
    async def publish(request: Request, repository: Repository = Depends(get_repo)) -> JSONResponse:
        return await _patch_endpoint(request, repository, "publish")

    return app

from __future__ import annotations

import json
import re
import struct
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.datastructures import UploadFile

from .auth import require_import_token
from .config import Settings
from .storage import (
    ContentNotFound,
    FileStorage,
)


TEMPLATE_PATH = Path(__file__).parent / "templates" / "render.html"
STATIC_PATH = Path(__file__).parent / "static"
DIFF_START_RE = re.compile(r"^(diff\b|---\s|\+\+\+\s|@@\s)")
MARKDOWN_RE = re.compile(r"^#{1,6}\s|\[[^\]]+\]\([^)]+\)|```|~~~|^\s*[-*]\s+", re.MULTILINE)
MARKDOWN_TABLE_RE = re.compile(r"^\s*\|?.+\|.+\|?\s*\n\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", re.MULTILINE)
MARKDOWN_MATH_RE = re.compile(r"\\\(|\\\[|\$\$")
ICON_PNG = None


def create_app(settings: Settings) -> FastAPI:
    storage = FileStorage(settings.storage_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage.ensure_root()
        yield

    app = FastAPI(title="Private Renderer Import Server", lifespan=lifespan)
    app.state.settings = settings
    app.state.storage = storage

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    def icon() -> Response:
        return Response(
            content=_icon_png(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/static/jsdiff.min.js", include_in_schema=False)
    def jsdiff() -> FileResponse:
        return FileResponse(
            STATIC_PATH / "jsdiff.min.js",
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/static/mathjax.min.js", include_in_schema=False)
    def mathjax() -> FileResponse:
        return FileResponse(
            STATIC_PATH / "mathjax.min.js",
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/")
    def root() -> dict[str, str]:
        return {
            "status": "ok",
            "health": "/health",
            "import": "/import",
        }

    @app.get("/api/content", response_class=PlainTextResponse)
    def api_content() -> PlainTextResponse:
        try:
            return PlainTextResponse(
                storage.get_content(),
                media_type="text/plain",
            )
        except ContentNotFound:
            raise HTTPException(status_code=404, detail="Content not found") from None

    @app.get("/api/meta")
    def api_meta() -> dict[str, object]:
        try:
            return storage.get_metadata()
        except ContentNotFound:
            raise HTTPException(status_code=404, detail="Content not found") from None

    @app.post("/import", dependencies=[Depends(require_import_token(settings.effective_import_token))])
    async def import_content(request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "")
        filename: str | None = None
        media_type: str | None = None

        if content_type.lower().startswith("multipart/form-data"):
            form = await request.form()
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise HTTPException(status_code=400, detail="Multipart body must include file field 'file'")
            payload = await file.read(settings.max_upload_bytes + 1)
            filename = file.filename
            media_type = file.content_type
        elif content_type.lower().startswith("application/json"):
            payload = await request.body()
            _reject_oversized(payload, settings.max_upload_bytes)
            try:
                data = json.loads(payload.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=400, detail="Request body must be valid UTF-8") from exc
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
            if not isinstance(data, dict) or not isinstance(data.get("content"), str):
                raise HTTPException(status_code=400, detail="JSON body must include string field 'content'")
            filename_value = data.get("filename")
            if filename_value is not None and not isinstance(filename_value, str):
                raise HTTPException(status_code=400, detail="JSON field 'filename' must be a string")
            text = data["content"]
            _reject_oversized(text.encode("utf-8"), settings.max_upload_bytes)
            filename = filename_value
            media_type = "application/json"
            storage.save(text, filename=filename, content_type="text/plain")
            return JSONResponse(_import_response(settings))
        else:
            payload = await request.body()
            media_type = content_type.split(";", 1)[0] or "text/plain"

        _reject_oversized(payload, settings.max_upload_bytes)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Imported content must be valid UTF-8 text") from exc

        storage.save(text, filename=filename, content_type=media_type or "text/plain")
        return JSONResponse(_import_response(settings))

    @app.get("/render")
    def render() -> HTMLResponse:
        try:
            content = storage.get_content()
        except ContentNotFound:
            raise HTTPException(status_code=404, detail="Content not found") from None
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        rendered = template.replace("__IMPORTED_CONTENT_JSON__", _script_json(content))
        rendered = rendered.replace("__IMPORTED_KIND_JSON__", json.dumps(detect_content_kind(content)))
        return HTMLResponse(rendered)

    return app


def _reject_oversized(payload: bytes, max_upload_bytes: int) -> None:
    if len(payload) > max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Upload exceeds {max_upload_bytes} bytes",
        )


def _import_response(settings: Settings) -> dict[str, str]:
    base_url = settings.base_url.rstrip("/")
    return {
        "url": f"{base_url}/render",
    }


def _script_json(value: str) -> str:
    return json.dumps(value).replace("</", "<\\/")


def detect_content_kind(text: str) -> str:
    text = text.lstrip("\ufeff")
    first_content_line = next(
        (line for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()),
        "",
    )
    if DIFF_START_RE.match(first_content_line):
        return "diff"
    if MARKDOWN_RE.search(text) or MARKDOWN_TABLE_RE.search(text) or MARKDOWN_MATH_RE.search(text):
        return "markdown"
    return "text"


def _icon_png(size: int = 180) -> bytes:
    global ICON_PNG
    if ICON_PNG is not None:
        return ICON_PNG

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            inset = size // 5
            paper = inset <= x < size - inset and inset // 2 <= y < size - inset // 2
            fold = x >= size - inset * 2 and y < inset * 2
            if paper and not fold:
                color = (245, 247, 250, 255)
            elif fold:
                color = (174, 205, 255, 255)
            else:
                color = (31, 81, 155, 255)
            row.extend(color)
        rows.append(bytes(row))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    ICON_PNG = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), level=9))
        + chunk(b"IEND", b"")
    )
    return ICON_PNG

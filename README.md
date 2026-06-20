# Private Renderer Import Server

A small FastAPI server for importing shared text, Markdown, or diff files from an iOS Shortcut and opening them in a private LAN renderer.

The server keeps one current import. Each new `POST /import` overwrites the previous content, so render URLs do not contain IDs or other URL tokens. The renderer displays Markdown as HTML, unified diffs with highlighted rows, and plain text in a readable preformatted view.

This is a private intranet tool. It is not hardened for public internet exposure. Keep it on a trusted LAN or behind your own private network controls.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"
```

## Configuration

Important environment variables:

```bash
RENDERER_IMPORT_TOKEN="change-me"
RENDERER_BASE_URL="https://192.168.1.50:8443"
RENDERER_TLS_CERT="./certs/server.crt"
RENDERER_TLS_KEY="./certs/server.key"
```

Optional variables:

```bash
RENDERER_HOST="0.0.0.0"
RENDERER_PORT="8443"
RENDERER_STORAGE_DIR="./data/imports"
RENDERER_RETENTION_HOURS="24"
RENDERER_MAX_UPLOAD_BYTES="10485760"
```

## HTTPS

iPhone and PWA use should use HTTPS. The certificate must be trusted by the iPhone. For IP-based HTTPS, the certificate must include the LAN IP address in the Subject Alternative Name.

Run with HTTPS:

```bash
RENDERER_IMPORT_TOKEN="change-me" \
RENDERER_BASE_URL="https://192.168.1.50:8443" \
RENDERER_TLS_CERT="./certs/server.crt" \
RENDERER_TLS_KEY="./certs/server.key" \
python -m renderer_server
```

For local testing only, HTTP dev mode is available:

```bash
RENDERER_DEV_HTTP=1 RENDERER_IMPORT_TOKEN="change-me" python -m renderer_server
```

If `RENDERER_DEV_HTTP=1` is set and `RENDERER_IMPORT_TOKEN` is omitted, the server uses `dev-token`.

## Health Check

```bash
curl http://localhost:8443/health
```

For HTTPS, use your configured `https://<LAN-IP>:8443/health` URL.

## Import Examples

Raw text:

```bash
curl -k \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: text/plain" \
  --data-binary @changes.diff \
  https://192.168.1.50:8443/import
```

JSON:

```bash
curl -k \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"content":"# Hello\n\nImported from Shortcut.","filename":"note.md"}' \
  https://192.168.1.50:8443/import
```

The response includes:

```json
{
  "url": "https://192.168.1.50:8443/render"
}
```

The current import is rendered at `GET /render`.

## iOS Shortcut

Configure a Shortcut like this:

1. Receive file or text from the Share Sheet.
2. Add "Get Contents of URL".
3. Method: `POST`.
4. URL: `https://<LAN-IP>:8443/import`.
5. Header: `Authorization: Bearer <token>`.
6. Request body: the shared file or text.
7. Open the returned `url`.

Shortcuts can send raw text, a file as the request body, or a multipart `file` upload.

## Tests

```bash
pytest
```

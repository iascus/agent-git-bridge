Build a small Python HTTPS intranet import server for a private Markdown/diff renderer.

## Goal

Implement the Python backend for this flow:

1. iOS Shortcut receives a shared text file / diff / Markdown file from ChatGPT.
2. Shortcut sends it to the server with `POST /import`.
3. Server overwrites the current stored content.
4. Server returns a JSON response containing a render URL.
5. Browser opens `/render`.
6. `/render` shows the latest imported content.

There is only **one current page**. Each new import replaces the previous content.

## Constraints

* Python only.
* Intranet/private LAN tool.
* No database.
* No content history.
* No random content IDs.
* No expiry cleanup.
* No user accounts.
* No OpenAI API.
* No native iOS app.
* Keep implementation small and readable.
* Use bearer-token authentication for `POST /import`.
* Renderer can be minimal for v1: raw `<pre>` display is acceptable, but structure it so Markdown/diff rendering can be added later.

## Suggested stack

Use:

* `FastAPI`
* `uvicorn`
* `python-multipart` if multipart upload support is implemented
* standard library modules for file storage, paths, JSON, SSL config, and token comparison

Default to FastAPI unless Flask would be much simpler.

## Required endpoints

### `POST /import`

Accept content from iOS Shortcut and overwrite the current stored page.

Support at least:

1. Raw body:

   * `Content-Type: text/plain`
   * body is the file/text content

2. JSON:

   * `Content-Type: application/json`
   * fields:

     * `content`: string
     * `filename`: optional string

Prefer also supporting:

3. Multipart file upload:

   * form field name: `file`
   * preserve uploaded filename in metadata if available

Authentication:

* Require:

  * `Authorization: Bearer <IMPORT_TOKEN>`
* If missing or wrong, return `401`.

Behaviour:

* Decode content as UTF-8 text.
* Reject invalid UTF-8 with `400`.
* Reject oversized uploads.
* Default max upload size: 10 MB.
* Atomically overwrite:

  * `data/current/content.txt`
  * `data/current/meta.json`
* Store metadata:

  * original filename if available
  * content type if available
  * updated timestamp
  * byte length
* Return JSON:

```json
{
  "url": "https://192.168.1.50:8443/render"
}
```

Do not hard-code the base URL. Use config.

### `GET /api/content`

Return the latest stored content as plain text.

Behaviour:

* If no content has been imported yet, return `404`.
* Return:

  * `text/plain; charset=utf-8`

No authentication required for v1, because this is a private intranet tool. Keep this documented.

### `GET /api/meta`

Return metadata for the latest import.

Example:

```json
{
  "filename": "changes.diff",
  "content_type": "text/plain",
  "updated_at": "2026-06-20T12:34:56Z",
  "size_bytes": 12345
}
```

If no content has been imported yet, return `404`.

### `GET /render`

Serve a simple HTML page.

For v1:

* Fetch `/api/content`.
* Fetch `/api/meta`.
* Display filename and updated timestamp if available.
* Display content in a `<pre>` block.
* Escape all content safely.
* Include placeholder JavaScript functions:

  * `detectContentType(text)`
  * `renderMarkdown(text)`
  * `renderDiff(text)`
  * `renderPlainText(text)`

For now, `renderMarkdown` and `renderDiff` may simply fall back to plain text. The frontend renderer can be improved separately.

### `GET /health`

Return:

```json
{"status": "ok"}
```

## Configuration

Use environment variables:

* `RENDERER_HOST`

  * default: `0.0.0.0`

* `RENDERER_PORT`

  * default: `8443`

* `RENDERER_BASE_URL`

  * default: `https://localhost:8443`
  * used to construct the returned render URL

* `RENDERER_IMPORT_TOKEN`

  * required unless `RENDERER_DEV_HTTP=1`
  * fail startup if missing in normal mode

* `RENDERER_STORAGE_DIR`

  * default: `./data/current`

* `RENDERER_MAX_UPLOAD_BYTES`

  * default: `10485760`

* `RENDERER_TLS_CERT`

  * path to TLS certificate

* `RENDERER_TLS_KEY`

  * path to TLS private key

* `RENDERER_DEV_HTTP`

  * if set to `1`, allow HTTP dev mode without TLS

## HTTPS

Support HTTPS through uvicorn SSL parameters.

Example run:

```bash
RENDERER_IMPORT_TOKEN="change-me" \
RENDERER_BASE_URL="https://192.168.1.50:8443" \
RENDERER_TLS_CERT="./certs/server.crt" \
RENDERER_TLS_KEY="./certs/server.key" \
python -m renderer_server
```

Allow HTTP only for local development:

```bash
RENDERER_DEV_HTTP=1 \
RENDERER_IMPORT_TOKEN="change-me" \
RENDERER_BASE_URL="http://localhost:8443" \
python -m renderer_server
```

README must state:

* iPhone/PWA use should use HTTPS.
* The iPhone must trust the certificate.
* For IP-based HTTPS, the certificate must include the LAN IP address in Subject Alternative Name.

## Storage layout

Use:

```text
data/current/
  content.txt
  meta.json
```

Requirements:

* Never use uploaded filenames as paths.
* Store filename only as metadata.
* Write updates atomically:

  * write to temporary files
  * then replace existing files
* Prevent partial writes where possible.
* Do not execute uploaded content.
* Do not serve directory listings.

## Security requirements

* Require bearer token for `POST /import`.
* Use constant-time comparison for token checks.
* Reject oversized uploads.
* Treat all imported content as untrusted text.
* Escape content in HTML.
* No public-internet hardening required, but README must clearly say this is a private LAN tool.

## iOS Shortcut compatibility

Make `POST /import` tolerant of Shortcuts sending:

* raw text body
* file as request body
* JSON body
* multipart file upload if easy

Expected Shortcut flow:

```text
Receive file/text from Share Sheet
↓
Get Contents of URL
  URL: https://<LAN-IP>:8443/import
  Method: POST
  Header:
    Authorization: Bearer <token>
  Request Body:
    File or Text from Shortcut Input
↓
Get Dictionary Value: url
↓
Open URL
```

## Project structure

Use a small structure:

```text
renderer_server/
  __init__.py
  __main__.py
  app.py
  config.py
  storage.py
  auth.py
  templates/
    render.html
tests/
  test_import.py
  test_storage.py
  test_auth.py
pyproject.toml
README.md
```

## Tests

Add `pytest` tests for:

1. `POST /import` rejects missing token.
2. `POST /import` rejects wrong token.
3. `POST /import` accepts raw text with correct token.
4. `POST /import` accepts JSON content with correct token.
5. Second import overwrites first import.
6. `/api/content` returns latest content.
7. `/api/meta` returns latest metadata.
8. `/api/content` returns `404` before first import.
9. Oversized upload is rejected.
10. Invalid UTF-8 is rejected.
11. HTML content is displayed safely and not executed in `/render`.

## README

Include:

1. Create venv.
2. Install dependencies.
3. Set environment variables.
4. Generate or provide TLS certificate.
5. Run server.
6. Test `/health`.
7. Import with `curl`.
8. Configure iOS Shortcut.

Example raw text curl:

```bash
curl -k \
  -X POST "https://localhost:8443/import" \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: text/plain" \
  --data-binary @example.diff
```

Example JSON curl:

```bash
curl -k \
  -X POST "https://localhost:8443/import" \
  -H "Authorization: Bearer change-me" \
  -H "Content-Type: application/json" \
  -d '{"filename":"note.md","content":"# Hello\n\nMarkdown text."}'
```

## Definition of done

The task is complete when:

* Server starts.
* `/health` returns OK.
* `POST /import` works with bearer token.
* Imported content overwrites previous content.
* `/render` opens the latest page.
* `/api/content` returns latest uploaded content.
* Tests pass.
* README explains HTTPS LAN setup and iOS Shortcut configuration.

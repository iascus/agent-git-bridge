# Viewer (`/viewer/*`)

A small companion feature, mounted on the same FastAPI app/port as the rest
of the bridge: an iOS Shortcut POSTs shared text, Markdown, or a diff, and a
browser opens a render URL to view it. There is only **one current page**;
each import overwrites the previous one. No content history, no IDs, no
accounts, no database.

Originally a separate project (`file-viewer`, package `renderer_server`),
merged into this repo so there is one service, one auth model, and one
Tailscale Serve entry to operate instead of two. The merge brought its full
git history in under `src/renderer_server/`; see the `renderer-server/`
subtree-add commit if you need that standalone history.

## Endpoints

| Method | Path | Auth |
|---|---|---|
| `POST` | `/viewer/import` | bearer token (same `server.bearer_token_file` as every other write) |
| `GET` | `/viewer/render` | none |
| `GET` | `/viewer/api/content` | none |
| `GET` | `/viewer/api/meta` | none |
| `GET` | `/viewer/health` | none |
| `GET` | `/viewer/static/*` | none |

The read routes are deliberately unauthenticated: `/viewer/render` is meant
to be opened as a plain link (tapped from a Shortcut's "Open URL", or a
browser bookmark), which cannot attach an `Authorization` header. Tailscale
is the access boundary for these, the same as it is for everything else this
server exposes — see [SECURITY.md](../SECURITY.md).

`POST /import` accepts raw text (any `Content-Type` other than
`multipart/form-data` or `application/json`), a JSON body
(`{"content": "...", "filename": "..."}`), or a multipart upload (field
`file`). It rejects invalid UTF-8 and uploads over `viewer.max_upload_bytes`
(default 10 MB), and returns `{"url": "<viewer.base_url>/viewer/render"}`.

## Configuration

```yaml
viewer:
  storage_dir: C:/Users/YOU/AppData/Local/agent-git-bridge/viewer
  base_url: https://cave.tail364856.ts.net:10000
  max_upload_bytes: 10485760   # optional, default shown
```

Omit the `viewer:` section entirely to run without it — no `/viewer/*`
routes are registered, same as any other optional feature here.

## iOS Shortcut

```text
Receive file/text from Share Sheet
  -> Get Contents of URL
       URL: <viewer.base_url>/viewer/import
       Method: POST
       Header: Authorization: Bearer <bearer_token_file contents>
       Request Body: File or Text from Shortcut Input
  -> Get Dictionary Value: url
  -> Open URL
```

## What changed from the standalone version

The standalone `renderer_server` terminated its own TLS (a self-signed
certificate trusted on the iPhone) so it could be reached directly by raw
LAN IP, independent of Tailscale, and ran as its own process on its own
port behind a separate `RENDERER_IMPORT_TOKEN`. All of that is retired:
this feature is now reachable only through the existing Tailscale Serve
route (same `:10000 -> 127.0.0.1:8000` entry the rest of the bridge uses),
authenticated with the same bearer token as everything else. There is no
more direct-LAN-IP access path.

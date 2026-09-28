# Architecture

`agent-git-bridge` is a narrowly scoped service that lets an AI conversation
(ChatGPT) **read exact Git snapshots** through Google Drive and **propose
commits** as data, while Git publication stays an explicit, user-authorised
action (an iOS Share Sheet tap).

It is not a Git host, a CI system, or a general remote-execution service.

## Trust boundary

```text
ChatGPT
   │
   │ generates data only (publish.zip)
   ▼
user-authorised iOS Share action
   │
   ▼
private HTTPS over Tailscale
   │
   ▼
strict Git bridge  ── holds GitHub + Google credentials, never exposes them
   │
   ▼
GitHub  (authoritative history)
```

ChatGPT never receives GitHub credentials, local paths, or direct write
authority. Everything it produces (patch, commit message, target SHA) is
treated as untrusted input and validated against server-side configuration.

## Data flows

### Pull

```text
                         PULL

GitHub
   │  git fetch (allowlisted branches only)
   ▼
Private Git Bridge
   │
   ├── persistent bare clone (cache of GitHub history)
   │
   └── snapshot export (reads Git objects of ONE commit)
          │
          │ individual files + snapshot.json (written last)
          ▼
      Google Drive
          │
          ▼
       ChatGPT  (normal Drive integration; never calls the bridge)
```

Reader protocol:

```text
conversation start
    ↓
read snapshot.json
read bootstrap files
read indexes
    ↓
later, as required
    ↓
read individual lazy files
```

### Push

```text
                         PUSH

ChatGPT
   │
   │ compressed publish.zip (request.json + changes.patch)
   ▼
iOS Share Sheet
   │
   ▼
iOS Shortcut  ("Publish Git Patch"; posts the file unchanged)
   │
   │ HTTPS over Tailscale + Bearer token
   ▼
Tailscale Serve
   │
   │ localhost HTTP
   ▼
Private Git Bridge
   │
   ├── validate archive / allowlist / checksum
   ├── fetch + verify expected SHA   (optimistic concurrency)
   ├── temporary worktree at that exact SHA
   ├── git apply --check, apply, policy checks, validation
   ├── commit
   └── push (fast-forward only, never forced)
          │
          ▼
        GitHub
          │
          ▼
   snapshot refresh → Google Drive   (failure here does not undo the push)
```

### Network exposure

```text
Internet
   X
   │
   │ no public write endpoint (no Funnel, no port forward)
   X

Tailnet client (iPhone)
   │
   │ HTTPS (TLS terminated by Tailscale Serve)
   ▼
Tailscale Serve  https://<machine>.<tailnet>.ts.net:10000
   │
   ▼
127.0.0.1:8000   (Uvicorn, plain HTTP, loopback only)
```

FastAPI never handles TLS. The listener is bound to `127.0.0.1`; binding to
`0.0.0.0` is not a supported production configuration.

## Components

| Module | Responsibility |
|---|---|
| `git_bridge.config` | Load and strictly validate YAML configuration (repositories, allowed branches, validation commands, export rules, limits). |
| `git_bridge.gitcmd` | The only place that executes Git. Fixed argument arrays, no shell, isolated from user/system Git config, timeouts, credentials injected via environment for network operations only. |
| `git_bridge.artifact` | Parse and validate `publish.zip` in memory (size/entry limits, exact member names, checksum, strict `request.json` schema). Never extracts to disk. |
| `git_bridge.repository` | Per-repository operations: ensure clone, fetch, status, isolated worktrees, validate/publish pipeline, refresh, publish-then-refresh. `Bridge` is the registry of configured repositories. |
| `git_bridge.snapshot` | Commit inventory (`ls-tree`, `cat-file --batch`), bootstrap/index/lazy classification, `snapshot.json` schema, two-phase export protocol. |
| `git_bridge.store` | `SnapshotStore` interface and `LocalDirectorySnapshotStore` (development, tests). |
| `git_bridge.drive` | `GoogleDriveSnapshotStore`, thin Drive v3 wrapper, OAuth login and token refresh. |
| `git_bridge.api` | FastAPI app: health, status, refresh, validate-patch, publish. |
| `git_bridge.auth` | `Authenticator` seam; bearer-token implementation. |
| `git_bridge.events` | Structured log events and the append-only audit log. |
| `git_bridge.cli` | `git-bridge` command: serve, check, init-token, google-login, status, refresh, validate, publish. |
| `git_bridge.results` | Compact, structured result models returned by every operation. |
| `git_bridge.errors` | Typed rejections with stable codes and HTTP status. |

## HTTP API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | `{"status":"ok"}`; nothing else |
| GET | `/repos/{repo}/status` | bearer | Remote head of each allowed branch, current snapshot state |
| POST | `/repos/{repo}/refresh` | bearer | Fetch and export the export branch head |
| POST | `/repos/{repo}/validate-patch` | bearer | Full publication checks, no commit |
| POST | `/repos/{repo}/publish` | bearer | Guarded commit + push, then snapshot refresh |

Upload bodies are the ZIP itself (`application/zip` or octet-stream) or a
`multipart/form-data` body with exactly one file; no base64/JSON wrapping.
Bodies are streamed with the size limit enforced before parsing. Response
formats and status codes: [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Git core

### Persistent clone

Each configured repository has one persistent **bare** clone at
`local_path`. It is a cache of GitHub history, not a working copy:

- Nothing is ever checked out in it, so no stray local edits, untracked files
  or half-finished merges can influence a publication.
- On startup the bridge verifies the clone's remote URL equals the configured
  one and refuses to operate otherwise.
- Only allowlisted branches are fetched, with an explicit refspec into
  `refs/remotes/<remote>/<branch>`.

GitHub is authoritative for history. The local clone is disposable
infrastructure: deleting it and re-cloning must always be safe.

### Multiple repositories

Several repositories (e.g. `rot3k` and `cyberpunk-tactics`) are configured
side by side under stable keys; the key is what appears in URLs
(`/repos/<key>/…`), Shortcuts and logs. Each repository has its **own** bare
clone, its own lock and its own Drive export folder, so:

- operations on different repositories run concurrently and never block each
  other;
- operations on the same repository are serialised (a second publication
  against the same base gets `409 remote_changed` rather than racing);
- temporary worktrees share `work_dir` but use unique per-repository names.

Configuration loading rejects setups where repositories are not independent:
the same GitHub repository (or former name) under two keys, overlapping or
nested `local_path`s, a clone inside `work_dir`, or overlapping
`export.drive_root`s. An artifact naming one repository is rejected by every
other repository's endpoint.

**Renames.** `github_repo` can change without changing the key. Listing the old
name in `former_github_repos` keeps accepting artifacts that still name it and
lets the bridge re-point an existing clone whose remote is exactly the old
GitHub URL. Any other remote URL mismatch is refused.

### Git invocation rules

- `subprocess.run([...], shell=False)` with argument lists built by server code.
- Values originating from requests (branch, SHA) are only used after they have
  been matched against configuration (branch) or validated as full-length
  lowercase hex (SHA). The configured string, not the request string, is used.
- `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<devnull>`, `GIT_TERMINAL_PROMPT=0`,
  `core.autocrlf=false`, and `GIT_CEILING_DIRECTORIES` so Git can never
  discover or operate on an unrelated parent repository.
- GitHub credentials are passed only to `fetch`/`push`/`clone`, through
  `GIT_CONFIG_*` environment variables (an `http.extraHeader`), never argv
  and never persisted into Git config.

### Publish pipeline

```text
parse publish.zip  ──► reject: 400/413 invalid archive, checksum mismatch
allowlist check    ──► reject: 403 repository / branch not allowed
[per-repo lock]
fetch branch
observed == expected_base_sha ? ──► reject: 409 remote_changed
git worktree add --detach <tmp> <expected_base_sha>
git apply --check --index       ──► reject: 422 patch_does_not_apply
git apply --index
policy on staged diff           ──► reject: 422 (empty, symlink, submodule, binary, denied path)
git diff --cached --check       ──► reject: 422 validation_failed
configured validation commands  ──► reject: 422 validation_failed
numstat (changed files, +/-)
── validate-patch stops here ──
git commit (server identity, message from request)
git push <remote> <new_sha>:refs/heads/<branch>   (no '+', no --force)
                                ──► 409 push_race (non-fast-forward)
                                ──► 502 push_rejected (hook / protection)
[finally] remove worktree
```

Push safety comes from ordinary fast-forward semantics: the new commit's only
parent is `expected_base_sha`, so the push succeeds only if the remote branch
still points at that commit (or, pathologically, at one of its ancestors —
see *Known limitations*).

### Validation commands

Configured commands run in the temporary worktree with a minimal environment
(no GitHub token, no Google credentials), a timeout, and captured, truncated
output. They are trusted configuration, but **the files they operate on come
from the untrusted patch**. A validator that executes repository content
(test runners, build scripts) effectively gives the patch author code execution
as the service user. Prefer validators that only *inspect* files (linters,
schema checkers), or run the service under a dedicated account with systemd
sandboxing. See `SECURITY.md`.

## Snapshot export

### Archive format (default)

Each repository is exported as **one deterministic ZIP** with a stable name
(`<key>-snapshot.zip`), containing `snapshot.json` and the selected files.
The Drive file is updated in place (same file ID and name), so a single write
switches readers from one commit to the next: there is no window in which
files from two commits coexist, and no `updating` marker is needed. An
unchanged commit and selection is detected by a content fingerprint and not
re-uploaded. A large repository costs one upload instead of one request per
file. Per-file exports left from the files format are trashed only after the
ZIP is in place.

### Selection

With `export.manifest`, the repository's own manifest (e.g.
`docs/design/MANIFEST.md`) defines the exported set (`project_source_files`)
and bootstrap/lazy classes (`project_source_materialization`), read from the
exported commit. Otherwise glob rules (`bootstrap`, `indexes`, `lazy`,
`exclude`) apply.

### Consistency model (files format)

A snapshot is generated from Git objects of one resolved commit
(`git ls-tree -r -z --long <sha>` + `git cat-file`), never from a working tree,
so one snapshot cannot mix commits.

Drive files are updated in place to keep stable paths. In-place updates alone
cannot give readers an atomic switch: while files are being replaced, the old
`snapshot.json` would describe files that already changed. The export
therefore uses a two-phase marker:

1. Write `snapshot.json` with `"state": "updating"`, the previous commit and
   the target commit. Readers must treat the snapshot as in flux.
2. Upload/update changed files (compare by blob SHA recorded in Drive
   `appProperties`, so unchanged files are skipped); delete removed files.
3. Write the final `snapshot.json` (`"state": "complete"`) **last**, with a new
   `generation_id`.

If step 2 fails, `snapshot.json` stays in `updating` state, which is honest;
a retry of `refresh` completes it.

### Drive layout

- Scope `https://www.googleapis.com/auth/drive.file`: the bridge can only see
  files it created.
- Repository-relative paths are mirrored as real Drive folders under
  `export.drive_root`, so ChatGPT's Drive integration shows the familiar tree.
- Each bridge-created file carries private `appProperties`: repository key,
  kind (`file`/`folder`/`snapshot`), Git blob SHA, class, and its repository
  path (split into ≤100-byte chunks because a property is limited to 124
  bytes). The Drive state is therefore **self-describing**: there is no local
  index that could drift, a fresh process rediscovers everything with one
  query, and duplicates left by an interrupted run are trashed.
- Files are updated in place (same file ID, name and folder). Removed files go
  to the Drive trash. `snapshot.json` records each file's `drive_file_id`.
- Text files only; binary, symlinked, oversized files are listed in
  `snapshot.json` with `exported: false` and a `reason`.

### Snapshot after publication

`publish` exports the **newly published commit** (which must be reachable
from the fetched branch head) after the push. A snapshot failure is reported
as `snapshot_refresh: "failed"` with `snapshot_error`, while
`git_publish: "success"` and HTTP 200 still stand; `refresh` can be retried
independently. Exports hold a separate per-repository lock so a slow Drive
upload does not block Git operations longer than necessary.

## Authentication

- Application layer: long random bearer token, compared in constant time,
  loaded from a file outside the repository. Auth is a pluggable dependency
  so it can later be replaced (e.g. Tailscale identity headers from Serve).
- GitHub: fine-grained PAT restricted to the configured repositories with
  *Contents: read and write* (a GitHub App installation token is a drop-in
  alternative). Withholding the *Workflows* permission prevents patches from
  changing `.github/workflows`.
- Google: OAuth installed-app flow, `drive.file` scope, refresh token stored
  outside the repository.

## Design decisions and deviations from the original specification

These change the letter of the spec where doing so materially improves
correctness, security or simplicity.

1. **Bare persistent clone.** The spec says "persistent clone"; a bare clone
   removes the whole class of "unrelated local state" problems and needs no
   checkout. Publication still uses a temporary worktree as specified.

2. **`git diff --check` is built in, not configured.** The patch is applied
   with `--index`, so a plain `git diff --check` (worktree vs index) would
   check nothing. The bridge always runs `git diff --cached --check` against
   the base; configured commands are additional.

3. **Staged-diff policy checks.** After applying, the bridge inspects the
   staged diff and rejects empty patches, symlinks, submodules (gitlinks),
   binary changes (MVP is text-only) and optional `denied_paths` globs.
   `git apply` already refuses paths outside the tree and inside `.git`.

4. **`patch_sha256` is integrity, not authenticity.** It lives in the same
   archive as the patch, so it only detects truncation/corruption in transit.
   Authorisation comes from the bearer token and the user's share action.

5. **`repository` in `request.json` must match the URL's `{repo}` key.** Both
   are checked; a mismatch is rejected rather than trusting either.

6. **Full-length SHAs only.** `expected_base_sha` must be a full 40- (or 64-)
   character lowercase hex object ID; abbreviations are ambiguous.

7. **Two-phase `snapshot.json`** (above), because "write the manifest last"
   alone does not stop readers seeing new files under an old manifest when
   files are updated in place.

8. **Google OAuth app publishing status.** OAuth clients in *Testing* status
   issue refresh tokens that expire after 7 days. For a long-running personal
   deployment the OAuth consent screen should be set to *In production*
   (`drive.file` is a non-sensitive scope and does not require verification).

9. **Single process, per-repository lock.** Run exactly one Uvicorn worker.
   Publications and refreshes for the same repository are serialised.

## Known limitations

- **Rewind race.** If, between the bridge's fetch and its push, someone
  force-pushes the branch *backwards* to an ancestor of `expected_base_sha`,
  the normal fast-forward push succeeds and re-publishes the dropped commits.
  A strict compare-and-swap would need `--force-with-lease=<ref>:<expected>`,
  which the spec forbids; the window is milliseconds and requires a force-push
  by another writer, so it is accepted.
- Validation commands are not sandboxed by the application (see above).
- One Uvicorn worker only; locks are in-process.
- A worktree left behind by a killed process is removed at the start of the
  next publication for that repository (worktree folders are named
  `<key>.<random>`; keys cannot contain `.`, so repositories never touch each
  other's folders).
- In the files format, the first export of a large repository takes minutes
  (one Drive API call per file); the archive format needs one upload.

## Deployment

Reference deployment: the Windows PC `cave` (Tailscale machine name), running
the bridge as a hidden per-user scheduled task and exposing it with
`tailscale serve --bg --https=10000 http://127.0.0.1:8000` next to an
unrelated existing Serve entry on port 443. A hardened systemd unit is
provided for Linux. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and
[docs/TAILSCALE_SETUP.md](docs/TAILSCALE_SETUP.md).

## Implementation phases

All six phases are implemented:

1. **Git core** — config, fetch/status, isolated worktree, validate, commit,
   guarded push.
2. **HTTP service** — FastAPI, bearer auth, loopback-only binding.
3. **Snapshot generation** — exact-commit inventory, blob SHAs,
   classification, two-phase manifest.
4. **Google Drive transport** — OAuth (`drive.file`), mirrored folders,
   in-place updates, refresh after publication.
5. **Tailscale deployment** — Serve on a dedicated HTTPS port, exposure
   verified (see `docs/TAILSCALE_SETUP.md`).
6. **iOS integration** — Share Sheet and refresh Shortcuts
   (`docs/IOS_SHORTCUTS.md`); the on-device end-to-end run is performed by the
   user.

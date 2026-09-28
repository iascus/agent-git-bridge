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
Tailscale Serve  https://git-bridge.<tailnet>.ts.net
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
| `git_bridge.repository` | Per-repository operations: ensure clone, fetch, status, isolated worktrees, validate/publish pipeline. |
| `git_bridge.results` | Compact, structured result model returned by every operation. |
| `git_bridge.errors` | Typed rejections with stable codes and suggested HTTP status. |
| *(Phase 2)* `git_bridge.api` | FastAPI app, bearer-token auth. |
| *(Phase 3)* `git_bridge.snapshot` | Commit inventory, classification, `snapshot.json` schema. |
| *(Phase 4)* `git_bridge.drive` | `SnapshotStore` abstraction and `GoogleDriveSnapshotStore`. |

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

## Snapshot export (Phases 3–4)

### Consistency model

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
- Repository-relative paths are mirrored as Drive folders under
  `export.drive_root`. The path → Drive file ID mapping is kept in local state
  and also recorded in `snapshot.json`, so readers and the bridge agree.
- Text files first; binary files are listed in `snapshot.json` but not
  exported in the MVP.

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

## Implementation phases

1. **Git core** — config, fetch/status, isolated worktree, validate, commit,
   guarded push, tests. *(done)*
2. HTTP service — FastAPI, bearer auth, loopback binding.
3. Snapshot generation.
4. Google Drive transport.
5. Tailscale Serve deployment.
6. iOS Shortcuts.

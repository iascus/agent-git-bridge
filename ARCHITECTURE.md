# Architecture

`agent-git-bridge` is a narrowly scoped service that gives an AI conversation
(ChatGPT) **exact Git source states** through Google Drive and accepts
**proposed changes** as data. Writing to Git stays an explicit, user-authorised
action (an iOS Share Sheet tap).

It is not a Git host, a CI system, or a general remote-execution service.

## Branch roles and source states

Each project has an integration branch and at least one working branch:

| Role | Example (Rot3K) | The bridge… |
|---|---|---|
| **integration branch** | `main` | reads it; exports it as the complete source snapshot; never writes to it |
| **working branch** | `design-docs` | exports it as a tree overlay on the integration snapshot; **Push** advances it |
| **additional working branch** (optional) | `gfx-assets` | same as a working branch, with its own pair, its own PR, exported only when it exists on the remote |

A repository may configure `additional_working_branches`: other branches a
push may target (`request.json.branch`, selecting among them), each
exported as its own, fully independent snapshot + overlay pair. The default
working branch stays the target when `branch` is omitted or `null`, so a
repository with no additional branches configured is byte-for-byte
unaffected by this. See [Additional working
branches](docs/PROTOCOL.md#additional-working-branches).

Implementation branches (e.g. `poc1`) are outside the bridge's model; they and
the working branch(es) integrate into the integration branch through pull
requests that the user merges.

ChatGPT therefore holds three trees:

```text
M = integration-branch tree      (snapshot ZIP)
D = working-branch tree          (M + working overlay)
H = ChatGPT's local HEAD         (D + new local changes)

Diff                  = D → H
Full diff             = M → H
Push                  = D → H      (expected_base_sha = working_commit)
Integration / PR diff = M → D      (the working overlay)
```

## Trust boundary

```text
ChatGPT
   │
   │ generates data only (<key>-push.zip)
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
authority. Everything it produces (patch, commit message, base SHA) is
untrusted input validated against server-side configuration.

## Data flows

### Pull (refresh)

```text
main ───────────────► complete source snapshot   <key>-snapshot.zip
 │
 └── tree diff ─────► working overlay ◄──── design-docs
                      <key>-working.diff
```

```text
GitHub
   │  one git fetch: integration + working branch
   ▼
Private Git Bridge ── persistent bare clone (cache of GitHub history)
   │
   │  resolve M and D together; build ONE generation:
   │    sel(M)        = files listed by M's manifest, from Git objects of M
   │    sel(D)        = files listed by D's manifest, from Git objects of D
   │    snapshot.zip  = snapshot.json + sel(M)
   │    working.diff  = diff-tree  tree(sel(M)) → tree(sel(D))
   ▼
Google Drive   (overlay written first, snapshot last)
   │
   ▼
ChatGPT  (normal Drive integration; never calls the bridge or GitHub)
```

### Push and integration

```text
ChatGPT Push
    ↓
working-branch patch  (<key>-push.zip: request.json + changes.patch, D → H)
    ↓
iOS Share Sheet → iOS Shortcut → HTTPS over Tailscale → Tailscale Serve
    ↓
agent-git-bridge
    ├── validate archive / repository / working branch / checksum
    ├── fetch; working head == expected_base_sha ?   (optimistic concurrency)
    ├── temporary worktree at that exact commit
    ├── git apply --check, apply, policy checks, validation
    ├── commit, push (fast-forward only, never forced)
    ├── open PR working → integration if none is open (optional)
    └── refresh: new generation from the current heads
    ↓
design-docs

design-docs
    ↓ PR  (merged by the user)
main
```

A push never touches the integration branch.

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

FastAPI never handles TLS. The listener must be a loopback address;
configuration refuses anything else.

## Refresh generations

### Selection

The repository's own manifest (`export.manifest`, e.g.
`docs/design/MANIFEST.md`) defines the project source set through its
`PROJECT_SOURCE_FILES` block. It is read **from each branch's own commit**: M's
manifest selects sel(M), D's manifest selects sel(D). Listed files that are
missing, binary, symlinks or larger than `max_file_bytes` are excluded on that
side and reported under `not_exported`. Implementation and build files not in
the manifest are never exported.

The same manifest may declare `PROJECT_ARTIFACT_ROOTS`: repository
directories whose every tracked Git file — whatever its content — is added to
the selection without being listed, stored and patched byte-for-byte (the
`binary` exclusion never applies to them; `symlink` and `too_large` still
do). This is how generated binary assets (e.g. `assets/gfx/`) join the same
snapshot/overlay/push pipeline as text source, with no bridge-specific code
per project — see [docs/PROTOCOL.md](docs/PROTOCOL.md#project-artifact-roots).

### The overlay is a tree diff

The overlay is **not** the working branch's commits and **not** "changes since
the merge base". It is computed between two synthetic trees built with a
throwaway index (`update-index --index-info` + `write-tree`) from exactly
sel(M) and sel(D), then `git diff-tree -p -M --full-index`. Consequently:

- `unzip(snapshot) + git apply(overlay) == sel(D)` byte for byte, including
  files added to or removed from the manifest itself, renames, deletions;
- if `main` advanced and `design-docs` has not been rebased, the overlay shows
  main-only changes being reverted. That is the true tree difference; the
  bridge never merges or rebases;
- after an external rebase the next refresh simply observes the new heads; if
  the working tree then equals the integration tree the overlay is empty.

`snapshot.json` also records `merge_base_commit`, `working_ahead_by` and
`working_behind_by` (informational; they never change the exported trees) and
`working_diff.working_files` (path → blob SHA of every file in sel(D)), so a
reader can verify its reconstruction.

### Consistency between the two artifacts

Both artifacts come from one resolved pair (M, D) fetched in a single
operation under the repository lock. They are separate Drive files with
stable names, so the bridge:

1. writes the overlay first (its header names the generation, M and D);
2. writes the snapshot ZIP last; its `snapshot.json` pins the overlay's
   `sha256`, `bytes`, `generation_id`, `base_commit` and `target_commit`.

A reader downloads the ZIP, then the overlay, and accepts them only if the
overlay's SHA-256 matches. Mid-refresh (new overlay, old ZIP) or after a
failed refresh the check fails and the reader fetches again; there is no state
in which a mismatched pair passes. An unchanged (M, D, selection) is detected
by a fingerprint and not re-uploaded.

### Drive layout

- Scope `drive.file`: the bridge only sees files and folders it created.
- `export.drive_root` (e.g. `ChatGPT/rot3k`) holds exactly two files,
  `<key>-snapshot.zip` and `<key>-working.diff`, each updated in place (stable
  Drive file ID and name) — plus, for each configured additional working
  branch, two more: `<key>-<branch>-snapshot.zip` and
  `<key>-<branch>-working.diff`, in the **same** folder.
- Private `appProperties` record the project key, artifact role and generation
  metadata; the Drive state is self-describing and a fresh process needs no
  local index. An additional branch's pair uses its own `gb_kind` value
  (`"<kind>@<branch>"`, e.g. `"archive@gfx-assets"`) so its files can never
  be found by, or collide with, the default pair's query — the default
  pair's own `gb_kind` values (`"archive"`, `"working_diff"`) are completely
  unchanged, so its existing Drive file IDs survive this feature's
  introduction untouched.

## Rebase after a squash merge

PRs are squash-merged: `main` gains one new commit S with the PR's changes,
while the working branch still carries the original commits. Without a
rebase, the next PR re-lists the merged work (GitHub diffs against the old
merge base) and history accumulates.

```text
before:  main ──●                      design-docs: ●──C1──C2──C3 (=P, PR head)
merge:   main ──●──S  (S = C1+C2+C3)   design-docs: ●──C1──C2──C3──C4
Refresh: main ──●──S                   design-docs: ●──S──C4'   (rebase --onto S P)
```

The bridge is not notified of merges (it is not reachable from GitHub).
Instead, **Refresh** checks the latest merged PR itself and, if needed and
enabled (`rebase_after_squash_merge`), rebases only the post-merge commits
onto the integration head, verifies the tree against `git merge-tree`, and
updates the working branch with a **lease-guarded force push** (the only
history rewrite the bridge performs, only for the working branch). A **push**
never rebases; it only warns while a rebase is pending. Conflicts change
nothing and are reported.

```text
merge PR on GitHub  ─►  (bridge not notified; Drive still shows the old state)
tap Refresh         ─►  rebase design-docs if needed ─► export new generation
push (any time)     ─►  commit + normal push ─► plain refresh (+ warning if pending)
```

## Components

| Module | Responsibility |
|---|---|
| `git_bridge.config` | Strict YAML configuration: repositories, branch roles (including `additional_working_branches`), validation commands, export, limits. |
| `git_bridge.gitcmd` | The only place that executes Git: fixed argv, no shell, isolated from user/system config, timeouts, credentials via environment for network operations only. |
| `git_bridge.artifact` | Parse and validate the push ZIP in memory (limits, exact member names, checksum, strict `request.json`; `branch` is optional). |
| `git_bridge.repository` | Per-repository clone, fetch, status, isolated worktrees, validate/push pipeline, refresh (default pair plus every additional working branch), push-then-refresh, PRs. `Bridge` is the registry. |
| `git_bridge.snapshot` | Manifest-based selection (source files + artifact roots), synthetic trees, binary-capable overlay, `snapshot.json` (format 2, or 3 with artifact roots), deterministic ZIP, ordered export. Branch-agnostic: called once per pair. |
| `git_bridge.manifest` | Parse the `PROJECT_SOURCE_FILES` and `PROJECT_ARTIFACT_ROOTS` blocks. |
| `git_bridge.store` / `drive` | `SnapshotStore` (two artifacts per pair — default or one additional branch — distinguished by an optional `branch` scope): local directory and Google Drive; OAuth. |
| `git_bridge.github` | Find/open pull requests (never merge). |
| `git_bridge.api` | FastAPI: health, status, refresh, validate-patch, push. |
| `git_bridge.auth` | `Authenticator` seam; bearer token. |
| `git_bridge.instance` | Single-instance handling for `serve`. |
| `git_bridge.events` | Structured logs and append-only audit log. |
| `git_bridge.cli` | `git-bridge` command. |

## HTTP API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | `{"status":"ok"}`; nothing else |
| GET | `/repos/{repo}/status` | bearer | Both default-pair branch heads, merge base, ahead/behind, current generation, plus a per-branch map for any configured additional working branches |
| POST | `/repos/{repo}/refresh` | bearer | Export a new generation for the default pair and every configured additional working branch that exists on the remote |
| POST | `/repos/{repo}/validate-patch` | bearer | All push checks, no commit |
| POST | `/repos/{repo}/push` | bearer | Guarded commit + push to the working branch, PR, refresh |
| POST | `/repos/{repo}/publish` | bearer | Deprecated alias of `/push` (kept for existing Shortcuts) |

Upload bodies are the ZIP itself or `multipart/form-data` with exactly one
file; no base64/JSON wrapping. See [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Git core

### Persistent clone

One persistent **bare** clone per repository at `local_path`, a disposable
cache of GitHub history: nothing is checked out, its remote URL must match
configuration, and only the two configured branches are fetched (explicit
refspecs into `refs/remotes/<remote>/<branch>`).

### Multiple repositories

Repositories are configured side by side under stable **project keys**
(`rot3k`, `cyberpunk-tactics`); the key is used in URLs, artifact names and
logs and is independent of the GitHub name. Each has its own clone, lock and
Drive folder; operations on different repositories run concurrently, on the
same repository they are serialised. Configuration rejects shared GitHub names,
overlapping clones or Drive roots, and a clone inside `work_dir`. Branch names
are per repository; nothing is hard-coded.

Within one repository, additional working branches deliberately share the
clone, the lock and the Drive folder with the default pair (not isolated the
way repositories are from each other): the git-level work for the default
pair and every additional branch happens under the same `self.lock` in one
`refresh()` call, serialised like any other git operation on that
repository, while each pair's own export (Drive/local upload) is isolated
from the others by its own try/except, so one branch's export failure can
never corrupt or block another's.

**Renames.** `github_repo` can change without changing the key;
`former_github_repos` keeps accepting push artifacts naming the old repository
and re-points a clone whose remote is exactly the old GitHub URL.

### Git invocation rules

- `subprocess.run([...], shell=False)` with argument lists built by server code.
- Request values (branch, SHA) are only used after matching configuration
  (the working branch) or validation as full-length lowercase hex; the
  configured string is used from then on.
- `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<devnull>`, `GIT_TERMINAL_PROMPT=0`,
  `core.autocrlf=false`, `GIT_CEILING_DIRECTORIES`; an allowlist of extra
  settings (`http.sslBackend`, …) for the host.
- The GitHub token reaches only `fetch`/`push`, as an HTTP header in
  `GIT_CONFIG_*` environment variables; never argv, never on disk.

### Push pipeline

```text
parse <key>-push.zip ──► 400/413 invalid archive, checksum mismatch
repository + branch  ──► 403 unless repository matches and branch resolves to an allowed working branch
                          (branch omitted/null -> the default working branch)
[per-repo lock]
fetch the resolved branch
head == expected_base_sha ? ──► 409 remote_changed
git worktree add --detach <tmp> <expected_base_sha>
git apply --check --index   ──► 422 patch_does_not_apply   (binary hunks included, no extra flag)
git apply --index
policy on staged diff       ──► 422 (empty, symlink, submodule, denied path)
git diff --cached --check   ──► 422 validation_failed      (nothing to check in binary content)
configured validation       ──► 422 validation_failed
── validate-patch stops here ──
git commit-tree (tree captured before validation)
git push <remote> <sha>:refs/heads/<working>   (no '+', no --force)
                            ──► 409 push_race / 502 push_rejected
[finally] remove worktree
then: PR (optional, from the resolved branch), refresh of every configured
      pair (default and additional) — failures there never fail the push
```

Concurrency is checked against the **resolved branch's** head, never the
integration head and never another working branch's head.

### Validation commands

Configured commands run in the temporary worktree with a scrubbed environment
and a timeout. **The files they operate on come from the untrusted patch**, so
prefer validators that only inspect files. See `SECURITY.md`.

## Authentication

- Application: long random bearer token, constant-time comparison; pluggable.
- GitHub: fine-grained PAT restricted to the configured repositories:
  *Contents: read and write*, plus *Pull requests: read and write* if PRs are
  enabled. No *Workflows* permission.
- Google: OAuth installed-app flow, `drive.file`, refresh token outside the
  repository; the consent screen must be *In production* (Testing tokens
  expire after 7 days).

## Design decisions

1. **Bare persistent clone**; pushes use a temporary worktree.
2. **Built-in `git diff --cached --check`** (the patch is applied with `--index`).
3. **Staged-diff policy**: no empty patches, symlinks, submodules, `denied_paths`. Binary
   add/modify/delete is allowed (`git apply` already handles Git binary patches
   with no extra flag); the policy is about file *mode* and *path*, never content.
4. **`patch_sha256` is integrity, not authenticity.**
5. **`request.json` repository must match the endpoint.**
6. **Full-length SHAs only.**
7. **Two source states, one generation**: the integration snapshot and the
   integration → working overlay are built from one resolved pair and pinned
   together by SHA-256; the overlay is a tree diff between manifest-selected
   trees, so reconstruction is exact whatever the history.
8. **Single process, per-repository locks**; `serve` stops earlier instances.
9. **Rebase after squash merge, on Refresh only**, lease-guarded, verified
   against `git merge-tree`, working branch only. Pushes never rewrite history.
10. **Project artifact roots are manifest-driven, like source selection**: a
    repository opts in by declaring `PROJECT_ARTIFACT_ROOTS` in its own
    manifest, not by a server-side configuration change, so new projects need
    no bridge code change. Git's own binary-patch format (`--binary`,
    `git apply`) carries both text and binary changes through the same
    overlay and push patch; no second parallel push format was added.
11. **Additional working branches reuse the single-branch pipeline per
    branch**, not a parallel implementation: `build_generation`/
    `export_generation` are unchanged; only the branch name, artifact names
    and (for Drive) the `gb_kind` value vary per call. A branch that does not
    exist on the remote is reported as skipped, never an error, and is never
    created by the bridge.

## Known limitations

- The post-merge rebase rewrites the working branch; local checkouts must
  `git pull --rebase` (or reset) afterwards.
- **Rewind race**: a force-push moving the working branch *backwards* to an
  ancestor of `expected_base_sha` between fetch and push would let a normal
  fast-forward push re-publish the dropped commits (closing it needs
  `--force-with-lease`, which is not used by design).
- Validation commands are not sandboxed by the application.
- One Uvicorn worker; locks are in-process.
- A binary file listed individually in `PROJECT_SOURCE_FILES` (not under a
  declared artifact root) is still excluded (`not_exported: "binary"`) and
  invisible to both artifacts, by design: artifact roots are the opt-in for
  binary content, not a blanket change to source-file selection.

## Deployment

Reference deployment: the Windows PC `cave`, a hidden per-user scheduled task,
exposed with `tailscale serve --bg --https=10000 http://127.0.0.1:8000`. A
hardened systemd unit is provided for Linux. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and
[docs/TAILSCALE_SETUP.md](docs/TAILSCALE_SETUP.md).

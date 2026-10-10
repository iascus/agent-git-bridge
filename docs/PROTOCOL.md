# Protocol

Two data formats cross the trust boundary:

- **Pull**: per project, a refresh *generation* the bridge writes to Google
  Drive: the integration-branch snapshot and the integration → working
  overlay (format version 2, or 3 if the project declares [project artifact
  roots](#project-artifact-roots)). A repository with [additional working
  branches](#additional-working-branches) gets one such pair per branch.
- **Push**: the ZIP ChatGPT produces to advance a working branch (the
  default one, or an explicitly named additional one).

Terminology: **Push** advances the working branch. **Integration** is the
working branch's changes entering the integration branch through a pull
request the user merges; the bridge never merges or rebases.

## Pull: snapshot + working overlay

Per project (key `rot3k` in the examples), `export.drive_root` holds exactly:

```text
My Drive/ChatGPT/rot3k/
├── rot3k-snapshot.zip      integration branch (M), complete source set
│   ├── snapshot.json       first entry; describes both states
│   ├── AGENTS.md
│   ├── docs/design/MANIFEST.md
│   └── …                   files listed by M's manifest
└── rot3k-working.diff      tree diff M → D (working branch), text patch
```

Both files keep a stable name and Drive file ID across refreshes; names can
be set with `export.snapshot_name` / `export.working_diff_name`.

### What is exported

The repository's manifest (`export.manifest`) lists the project source set in
its `PROJECT_SOURCE_FILES` block. Each branch is read with **its own**
manifest: sel(M) from M's manifest, sel(D) from D's. Listed files that are
missing, binary, symlinks or too large are excluded and reported in
`not_exported` (reasons `missing`, `binary`, `symlink`, `too_large`,
`unsupported_path`).

The same manifest may also declare **project artifact roots**: repository
directories whose every tracked Git file is exported byte-for-byte, text or
binary, without listing each file. See [Project artifact
roots](#project-artifact-roots).

### `snapshot.json` (format_version 2)

```json
{
  "format_version": 2,
  "generation_id": "20260930T101500Z-3e7e51c0-237852e3-a1b2c3",
  "generated_at": "2026-09-30T10:15:00Z",
  "repository": "iascus/rt3k",
  "project_key": "rot3k",

  "integration_branch": "main",
  "integration_commit": "3e7e51c0…",
  "working_branch": "design-docs",
  "working_commit": "237852e3…",
  "merge_base_commit": "3e7e51c0…",
  "working_ahead_by": 4,
  "working_behind_by": 0,

  "manifest_path": "docs/design/MANIFEST.md",
  "files": {
    "AGENTS.md": {"blob_sha": "…", "size": 5120}
  },
  "not_exported": {"docs/missing.md": "missing"},

  "working_diff": {
    "filename": "rot3k-working.diff",
    "sha256": "…",
    "bytes": 18234,
    "base_commit": "3e7e51c0…",
    "target_commit": "237852e3…",
    "empty": false,
    "files_changed": 3,
    "insertions": 120,
    "deletions": 14,
    "working_files": {"AGENTS.md": "<blob sha>", "…": "…"},
    "working_not_exported": {}
  },
  "reader_notes": "…"
}
```

| Field | Meaning |
|---|---|
| `integration_commit` (M) | The single commit every file in the ZIP comes from. |
| `working_commit` (D) | The working-branch head; **the base for pushes**. |
| `merge_base_commit`, `working_ahead_by`, `working_behind_by` | Branch relationship, informational only. |
| `files` | sel(M): path → Git blob SHA (`git rev-parse M:path`) and size. |
| `working_diff.sha256` / `bytes` | Identify the overlay file of the same generation. |
| `working_diff.working_files` | sel(D): path → blob SHA, to verify a reconstruction. |
| `working_diff.empty` | true when sel(D) == sel(M); the overlay has no patch body. |

### `<key>-working.diff`

A deterministic patch (`git diff-tree -p -M --full-index --binary`, prefixes
`a/` and `b/`) from the tree of sel(M) to the tree of sel(D), preceded by a
comment header that `git apply` ignores:

```text
# agent-git-bridge working-branch overlay
# generation: 20260930T101500Z-3e7e51c0-237852e3-a1b2c3
# repository: iascus/rt3k
# base: main 3e7e51c0…
# target: design-docs 237852e3…
# apply to the unzipped snapshot with: git apply

diff --git a/docs/design/RULES.md b/docs/design/RULES.md
…
```

It is the exact **tree** difference, not the working branch's commits and not
"changes since the merge base". If `main` has advanced and the working branch
has not been rebased, the overlay legitimately shows main-only changes being
reverted. After an external rebase, the next refresh reflects the new heads.

`--binary` makes a changed binary file (see [Project artifact
roots](#project-artifact-roots)) appear as a literal/base85 **GIT binary
patch** block instead of "Binary files differ"; `git apply` reconstructs it
the same way as a text hunk, with no extra flag needed. It is a byte-for-byte
no-op on an all-text diff, so format-2 projects are unaffected.

**Invariant:** unzipping the snapshot and applying the overlay reproduces
sel(D) byte for byte (verified by `working_diff.working_files`), whether
sel(D) contains binary files or not.

### Project artifact roots

A manifest may declare a second, optional block for repository directories
whose every tracked Git file — text or binary — is exported as-is, without
being listed individually:

```text
<!-- PROJECT_ARTIFACT_ROOTS_BEGIN -->
```yaml
project_artifact_roots:
  - assets/gfx/
```
<!-- PROJECT_ARTIFACT_ROOTS_END -->
```

Each root is read from the same commit as `PROJECT_SOURCE_FILES`, is
repository-relative, and is rejected (export fails closed) if it is absolute,
contains `..`, or otherwise fails the same traversal check applied to every
listed source path. A root matching no tracked files (or the block being
absent) is not an error. Files under a root are still subject to
`max_file_bytes` / `max_total_bytes` and are excluded (as `symlink` or
`too_large`, reported in `not_exported`) under the same rules as listed
source files — **except** the `binary` exclusion, which never applies to
artifact-root files. A path also listed in `PROJECT_SOURCE_FILES` that falls
under a root is treated as an artifact (included whether text or binary).

Declaring at least one artifact root (in either branch's manifest) raises
`format_version` to **3** and adds:

| Field | Meaning |
|---|---|
| `artifact_roots` | M's own declared roots (sorted), top level. |
| `files[path].origin` | `"source"` or `"artifact_root"`, present on every entry. |
| `working_diff.working_artifact_roots` | D's own declared roots (sorted). |
| `working_diff.working_artifact_files` | Paths of `working_diff.working_files` that are artifacts. |

A format-2 project (no manifest ever declares a root) is byte-for-byte
unaffected by any of this: the diff command's `--binary` flag is already a
no-op on text-only trees, and none of the fields above are added.

### Reader procedure

1. Download `<key>-snapshot.zip`, unzip, read `snapshot.json`.
2. Download `<key>-working.diff`. Check `sha256(file) ==
   working_diff.sha256` (and the `# generation:` line). On mismatch a refresh
   is in progress or failed: download both again (or ask the user to run
   *Refresh Git Snapshot*). Never combine files from different generations.
3. Keep the unzipped tree as **M**. Copy it, `git apply` the overlay (skip if
   `working_diff.empty`): that is **D**. Work on a copy of D: that is **H**.
4. Derived views: Diff = D → H; Full diff = M → H; Integration/PR diff =
   M → D (the overlay); Push = D → H.

### Export order and consistency

A refresh resolves M and D with one fetch, builds both artifacts from that
pair, writes the overlay first and the snapshot last. The snapshot pins the
overlay's SHA-256, so a half-finished or failed refresh is always detectable.
An unchanged (M, D, selection) is not re-uploaded.

### Additional working branches

`repositories.<key>.additional_working_branches` names other branches a push
may target, each independently exported as its own pair in the **same**
`drive_root` folder:

```text
My Drive/ChatGPT/cyberpunk-tactics/
├── cyberpunk-tactics-snapshot.zip               default pair (design-docs)
├── cyberpunk-tactics-working.diff
├── cyberpunk-tactics-gfx-assets-snapshot.zip     gfx-assets' own pair
└── cyberpunk-tactics-gfx-assets-working.diff
```

Naming: `<key>-<branch>-snapshot.zip` / `<key>-<branch>-working.diff`
(fixed, not configurable, unlike the default pair's `export.snapshot_name` /
`working_diff_name`). Each pair's `snapshot.json` is a complete, independent
generation — its own `generation_id`, fingerprint, Drive/local artifact
identity and overlay SHA-256 link — built from the **same** integration
branch head as the default pair's, so M's identity agrees across every pair
produced by one refresh, but reconstructing or verifying one pair never
requires the other. `snapshot.json.additional_working_branches` (when the
list is non-empty) advertises the full configured list, so a reader of any
one pair can discover the others' expected file names.

A branch is never created automatically. A refresh (`POST .../refresh`) or
`GET .../status` that finds a configured additional branch missing on the
remote reports it as `skipped_missing` for that branch — the default pair
still exports normally, and this is not an error. A transport or Git failure
while handling an *existing* additional branch is reported as `failed` for
that branch only (with its own `error`), distinctly from `skipped_missing`,
and never affects the default pair or any other additional branch:

```json
"additional_branches": {
  "gfx-assets": {
    "branch": "gfx-assets", "state": "ok", "working_commit": "…",
    "generation_id": "…", "uploaded": true,
    "snapshot_name": "cyberpunk-tactics-gfx-assets-snapshot.zip",
    "working_diff": {"filename": "cyberpunk-tactics-gfx-assets-working.diff", "…": "…"},
    "rebase": {"state": "not_needed"}, "error": null
  }
}
```

(`GET .../status`'s per-branch entries are narrower: `branch`, `state`,
`commit`, `generation_id`, `snapshot_name`, `working_diff_name`, `error`.)

Existing Shortcuts and readers that only look at the top-level, default-pair
fields are unaffected: `additional_branches` is purely additive on both
`refresh` and `status`.

## Push: the push artifact

```text
<key>-push.zip            e.g. rot3k-push.zip
├── request.json
└── changes.patch         working tree D → ChatGPT HEAD H
```

Exactly these two members, at the top level, stored or deflated, not
encrypted.

### `request.json` (format_version 1)

```json
{
  "format_version": 1,
  "repository": "iascus/rt3k",
  "branch": "design-docs",
  "expected_base_sha": "<working_commit from snapshot.json>",
  "patch_sha256": "<sha256 of changes.patch>",
  "commit_message": "Refine encounter activation UI",
  "created_at": "2026-09-30T10:20:00Z",
  "generator": "chatgpt"
}
```

| Field | Rules |
|---|---|
| `repository` | `owner/name`; the endpoint's repository (or a declared former name). |
| `branch` | Optional; `null` or omitted selects the default working branch. Otherwise must be the default working branch or one of `additional_working_branches` — any other value (including the integration branch) is refused. Whichever branch is selected determines the push target, PR head and rebase-after-squash-merge check; it never affects `repository` or `expected_base_sha`'s own rules. |
| `expected_base_sha` | Full hex commit ID; must equal the **selected** branch's real current head (its own pair's `working_commit`), never `integration_commit` and never another branch's `working_commit`. |
| `patch_sha256` | SHA-256 of `changes.patch`; detects corruption only. |
| `commit_message` | Non-blank, no NUL, ≤ 16 KiB. |
| `created_at`, `generator` | Optional, informational. |

A client that only ever uses the default branch needs no change at all:
omitting `branch`, or sending it explicitly as before, both work.

Unknown fields and duplicate keys are rejected. `changes.patch` is a `git
diff` from D (paths `a/…`, `b/…`), produced with `--binary` so it may contain
binary (added/modified/deleted) files alongside text changes in one patch —
`git apply` applies both from the same invocation, so no second artifact
member or protocol version is needed. Symlinks, submodules and
`denied_paths` are still rejected regardless of file content; text additions
must still pass `git diff --check` (binary content has nothing to check).
`request.json`'s shape is unchanged: a client that never produces binary
changes needs no changes at all, and nothing about the request format
signals whether a given patch happens to contain any.

### Limits (defaults)

Upload 5 MiB compressed, 20 MiB decompressed, 16 MiB per member, 8 entries,
`request.json` 64 KiB.

### Endpoints and responses

`POST /repos/<key>/push` (deprecated alias: `/publish`), `POST
/repos/<key>/validate-patch` (checks only), `POST /repos/<key>/refresh`,
`GET /repos/<key>/status`.

Push response:

```json
{
  "ok": true,
  "operation": "push",
  "repository": "iascus/rt3k",
  "branch": "design-docs",
  "expected_base_sha": "…", "observed_sha": "…", "old_sha": "…", "new_sha": "…",
  "changed_files": [{"path": "docs/x.md", "insertions": 3, "deletions": 1}],
  "files_changed": 1, "insertions": 3, "deletions": 1,
  "validation": {"passed": true, "checks": [{"name": "git diff --check", "passed": true, "exit_code": 0, "duration_ms": 12, "output_tail": ""}]},
  "git_push": "success",
  "pull_request": {"state": "created", "number": 12, "url": "https://github.com/iascus/rt3k/pull/12", "base": "main", "error": null},
  "snapshot_refresh": "success",
  "snapshot_generation_id": "…",
  "snapshot_error": null,
  "error": null,
  "message": "Pushed 0a1b2c3d4e5f to iascus/rt3k@design-docs: 1 file(s), +3 -1. Opened PR #12 into main: … Drive snapshot updated."
}
```

The response's `branch` is always the **resolved** branch name (never
`null`), whether the request named it explicitly or omitted it. After a
successful push the bridge (optionally) opens a PR from that branch into the
integration branch if none is open, then refreshes **every** configured
pair — the pushed-to branch's and every other one — from the heads as they
are now. Failures in either step are reported (`pull_request.state:
"failed"`, `snapshot_refresh: "failed"`) and never turn `git_push: "success"`
into a failure; a refresh failure specific to a *different* branch's pair
than the one just pushed to shows up under that branch's own entry in the
refresh response, never as this push's own failure.

A push **never rebases**. If the working branch still contains a PR that
was squash-merged into the integration branch, the push response carries
`"rebase": {"state": "needed", "pull_request": 47, …}` and its `message`
ends with `WARNING: design-docs still contains squash-merged PR #47; run
Refresh Git Snapshot to rebase it.`

### Rebase after a squash merge (Refresh)

With `rebase_after_squash_merge: true`, this applies independently to the
default working branch and to every configured additional branch: each
checks the latest merged PR **from its own branch** → integration, and a
rebase only ever rewrites that one branch. Rebasing the default branch after
its PR was squash-merged never touches an additional branch's branch or
pair, and vice versa.

A refresh first checks the latest **merged** PR working → integration
(GitHub API). If its head commit P is
still in the working branch's history, is *not* in the integration branch's
history (i.e. it was squash-merged), and its squash commit is in the
integration branch, the refresh:

1. rebases only the commits after P onto the integration head
   (`git rebase --onto <integration> P`) in a temporary worktree; with no
   post-merge commits the working branch simply becomes the integration head;
2. requires the rebased tree to equal `git merge-tree` of the two heads (so
   no content can be lost or reverted);
3. updates the working branch with `--force-with-lease=<branch>:<old head>`
   (never the integration branch);
4. exports the new generation.

A conflict or any failure changes nothing; the refresh still exports and
reports it. The refresh response gains:

```json
"rebase": {
  "state": "rebased",
  "pull_request": 47,
  "merged_head": "e586352835…",
  "old_commit": "5751017631…",
  "new_commit": "9c1d…",
  "replayed_commits": 1,
  "conflicts": [],
  "error": null
}
```

| `rebase.state` | Meaning | `message` prefix |
|---|---|---|
| `not_needed` | nothing to do | (none) |
| `rebased` | working branch rewritten onto the integration head | `Rebased design-docs onto main after PR #47 (1 commit(s) replayed).` |
| `needed` | pending, but rebasing is disabled for the repository | `WARNING: … rebasing is disabled …` |
| `conflict` | would conflict; nothing changed | `WARNING: rebasing design-docs after PR #47 conflicts (files); … rebase it manually.` |
| `failed` | GitHub lookup, verification or lease push failed; nothing changed | `WARNING: rebase of design-docs failed: …` |

After a rebase, local checkouts of the working branch (e.g. VS Code) must be
updated with `git pull --rebase` or reset to `origin/<working branch>`.

Refresh response (abridged):

```json
{
  "ok": true, "operation": "refresh",
  "repository": "iascus/rt3k", "key": "rot3k",
  "integration_branch": "main", "integration_commit": "…",
  "working_branch": "design-docs", "working_commit": "…",
  "merge_base_commit": "…", "working_ahead_by": 4, "working_behind_by": 0,
  "generation_id": "…", "uploaded": true,
  "snapshot_name": "rot3k-snapshot.zip", "file_count": 1430,
  "working_diff": {"filename": "rot3k-working.diff", "sha256": "…", "bytes": 18234, "empty": false, "files_changed": 3, "insertions": 120, "deletions": 14},
  "rebase": {"state": "not_needed", "…": "…"},
  "message": "iascus/rt3k: main 3e7e51c0… (1430 file(s)) + design-docs 237852e3… (overlay 3 file(s) +120 -14); uploaded."
}
```

| HTTP | `error.code` | Meaning |
|---|---|---|
| 200 | — | Success |
| 400 | `invalid_artifact`, `patch_checksum_mismatch` | Archive or `request.json` invalid |
| 401 | `unauthorized` | Missing or wrong bearer token |
| 403 | `not_allowed` | Wrong repository, or branch is not an allowed working branch |
| 404 | `unknown_repository` | No such project key |
| 409 | `remote_changed` | Working head ≠ `expected_base_sha`; refresh and regenerate |
| 409 | `push_race` | Working branch moved between check and push |
| 413 | `artifact_too_large` | Size limit exceeded |
| 422 | `patch_does_not_apply`, `patch_policy_violation`, `validation_failed` | Nothing committed |
| 502 | `push_rejected`, `snapshot_failed` | Remote refused / Drive export failed |

## Instructions for the AI assistant

Paste into the ChatGPT project instructions (adjust names):

> **Source states.** The project is in Google Drive under `ChatGPT/<key>/`:
> `<key>-snapshot.zip` (integration branch, complete) and `<key>-working.diff`
> (tree diff integration → working branch). Download both. Unzip the snapshot
> and read `snapshot.json`. Verify that the SHA-256 of the diff file equals
> `working_diff.sha256`; if not, stop and ask me to run *Refresh Git
> Snapshot*. Keep the unzipped files as M. Apply the diff with `git apply`
> (skip if `working_diff.empty`) to get D, and check D's files against
> `working_diff.working_files` (Git blob SHA-1). Make changes in a copy of D:
> that is your HEAD H. Never query GitHub for branch state.
>
> **Views.** "Diff" = D → H. "Full diff" = M → H. "Integration diff" = M → D
> (the overlay). If `working_behind_by` > 0, the working branch lacks recent
> integration changes; the overlay then shows them reverted. Mention it; do
> not try to merge.
>
> **Push.** When I ask to push, produce `<key>-push.zip` containing exactly
> `changes.patch` (`git diff --binary` D → H, paths `a/` and `b/`, no trailing
> whitespace on added text lines; binary files under an artifact root are
> included as Git binary patch hunks, same file, same member) and
> `request.json` with `format_version` 1, `repository` (from `snapshot.json`),
> `branch` = `working_branch`, `expected_base_sha` = `working_commit`,
> `patch_sha256` (SHA-256 hex of the exact `changes.patch` bytes),
> `commit_message`. Build it with Python's `zipfile` and give me the file. I
> push it from the share sheet.

If the project configures `additional_working_branches`, add a paragraph
naming each one and when to use it instead of the default, e.g. for a
project with `design-docs` (default) and `gfx-assets`:

> **Choosing a branch.** Default to `design-docs`. If the task clearly
> creates or revises GFX artwork/assets under `assets/gfx/`, use `gfx-assets`
> instead: download **its own** pair (`<key>-gfx-assets-snapshot.zip` +
> `<key>-gfx-assets-working.diff`) and set `request.json`'s `branch` to
> `"gfx-assets"` and `expected_base_sha` to **that pair's**
> `working_commit` — never mix a base SHA from one branch's pair with the
> other branch's `request.json`. If `gfx-assets` does not exist yet, say so;
> do not fall back to `design-docs` silently or claim it exists.

Example generator:

```python
import hashlib, json, zipfile

patch = open("changes.patch", "rb").read()          # D -> H
snapshot = json.load(open("M/snapshot.json"))
request = {
    "format_version": 1,
    "repository": snapshot["repository"],
    "branch": snapshot["working_branch"],
    "expected_base_sha": snapshot["working_commit"],
    "patch_sha256": hashlib.sha256(patch).hexdigest(),
    "commit_message": "Refine encounter activation UI",
    "generator": "chatgpt",
}
with zipfile.ZipFile(f"{snapshot['project_key']}-push.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("request.json", json.dumps(request, indent=2))
    zf.writestr("changes.patch", patch)
```

## Migration from format 1

- Configuration: replace `allowed_branches` with `integration_branch` and
  `working_branch`; `export` keeps `drive_root` and `manifest` (required);
  `format`, glob rules, `archive_name` and `pull_request.base` are gone
  (PRs always target the integration branch; `pull_request: {}` enables them).
- Drive: `<key>-snapshot.zip` keeps its file ID but now contains the
  integration branch with a format-2 `snapshot.json`; `<key>-working.diff` is
  new.
- Push: endpoint `/push` (the old `/publish` path still works for now);
  response field `git_push` replaces `git_publish`; `expected_base_sha` is
  the `working_commit`.

## Migrating to format 3 (project artifact roots)

Nothing to do for a project that never adds a `PROJECT_ARTIFACT_ROOTS` block:
`snapshot.json` stays `format_version: 2`, byte-for-byte as before.

To opt in, a consuming repository adds the block to its own manifest (both
branches, or the next commit that reaches each branch) — no bridge
configuration change, no server restart. The next refresh then produces
`format_version: 3` with the extra fields listed in [Project artifact
roots](#project-artifact-roots). Existing readers that only look at fields
present in format 2 keep working unmodified, since nothing already present is
removed or repurposed; a reader that wants byte-identical binary files needs
to treat the overlay's binary hunks the same way it already treats text
hunks (plain `git apply`, no new flag).

**iOS Shortcut: no change required.** Both Shortcuts (`Push Git Patch`,
`Refresh Git Snapshot`) already forward the ZIP body unmodified and never
inspect `changes.patch` or `working.diff` content; a push containing binary
files is indistinguishable, at the Shortcut layer, from one that does not.

## Migrating to additional working branches

Nothing to do for a repository that never configures
`additional_working_branches`: `request.json.branch` was always required
before and still works exactly as sent; `refresh`/`status` responses are
unchanged (an empty `additional_branches: {}`).

To opt in, add `additional_working_branches` to the repository's bridge
configuration (not the manifest — this is a server-side policy about which
branches a push may target, unlike [project artifact
roots](#project-artifact-roots), which a repository opts into through its
own manifest). No branch is created by this: the next refresh or status call
simply reports it `skipped_missing` until it exists on GitHub.

**iOS Shortcut: no change required for the default branch.** `Push Git
Patch` already forwards `request.json` unmodified, so a ChatGPT-produced
push naming an additional branch works through the existing Shortcut
unchanged. A deployment that wants a human-friendly "which branch?" picker
in the Shortcut itself (rather than leaving the choice to the ChatGPT
project instructions) can add a **Choose from Menu** step setting a `Branch`
text variable folded into the generated `request.json` before upload — this
is optional UI sugar, not a protocol requirement, and is independent of
ChatGPT's own branch choice described above.

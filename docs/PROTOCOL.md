# Protocol

Two data formats cross the trust boundary:

- **Pull**: per project, a refresh *generation* the bridge writes to Google
  Drive: the integration-branch snapshot and the integration → working
  overlay (format version 2).
- **Push**: the ZIP ChatGPT produces to advance the working branch.

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

A deterministic text patch (`git diff-tree -p -M --full-index`, prefixes
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

**Invariant:** unzipping the snapshot and applying the overlay reproduces
sel(D) byte for byte (verified by `working_diff.working_files`).

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
| `branch` | Must be the configured **working branch**. Pushes to the integration branch are refused. |
| `expected_base_sha` | Full hex commit ID; must equal the working branch head (`working_commit`), not `integration_commit`. |
| `patch_sha256` | SHA-256 of `changes.patch`; detects corruption only. |
| `commit_message` | Non-blank, no NUL, ≤ 16 KiB. |
| `created_at`, `generator` | Optional, informational. |

Unknown fields and duplicate keys are rejected. `changes.patch` is a `git
diff` from D (paths `a/…`, `b/…`), text files only; symlinks, submodules,
binary patches and `denied_paths` are rejected; added lines must pass `git
diff --check`.

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

After a successful push the bridge (optionally) opens a PR working →
integration if none is open, then refreshes from the heads as they are now
(the new working head and the current integration head). Failures in either
step are reported (`pull_request.state: "failed"`, `snapshot_refresh:
"failed"`) and never turn `git_push: "success"` into a failure.

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
  "message": "iascus/rt3k: main 3e7e51c0… (1430 file(s)) + design-docs 237852e3… (overlay 3 file(s) +120 -14); uploaded."
}
```

| HTTP | `error.code` | Meaning |
|---|---|---|
| 200 | — | Success |
| 400 | `invalid_artifact`, `patch_checksum_mismatch` | Archive or `request.json` invalid |
| 401 | `unauthorized` | Missing or wrong bearer token |
| 403 | `not_allowed` | Wrong repository, or branch is not the working branch |
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
> `changes.patch` (unified `git diff` D → H, paths `a/` and `b/`, text files
> only, no trailing whitespace on added lines) and `request.json` with
> `format_version` 1, `repository` (from `snapshot.json`), `branch` =
> `working_branch`, `expected_base_sha` = `working_commit`, `patch_sha256`
> (SHA-256 hex of the exact `changes.patch` bytes), `commit_message`. Build it
> with Python's `zipfile` and give me the file. I push it from the share sheet.

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

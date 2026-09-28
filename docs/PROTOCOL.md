# Protocol

Two data formats cross the trust boundary: the **snapshot** the bridge
exports to Google Drive (pull), and the **publication artifact** ChatGPT
produces (push). Both are versioned with `format_version`.

## Pull: the Drive snapshot

### Archive format (default)

One ZIP per repository with a **stable name**, replaced in place (same Drive
file ID) in a single write, so a reader always gets one complete commit:

```text
My Drive/
└── ChatGPT/rot3k/
    └── rot3k-snapshot.zip         name: export.archive_name, default <key>-snapshot.zip
        ├── snapshot.json          first entry; describes every file in the ZIP
        ├── AGENTS.md              selected files at their repository paths
        ├── docs/design/MANIFEST.md
        └── …
```

- The ZIP is deterministic (fixed timestamps, sorted entries). If commit and
  file selection are unchanged, it is not re-uploaded.
- `snapshot.json` inside the ZIP has the schema below with `state:
  "complete"` (always), `"archive": "<name>"`, and no `drive_file_id`s.
- Drive `appProperties` of the ZIP (private to the bridge) record commit,
  generation ID, SHA-256, size and a content fingerprint.
- Switching from the files format moves the per-file exports to the Drive
  trash after the ZIP has been written.

Reader: download the ZIP, unzip, read `snapshot.json`, then the `bootstrap`
files; open `lazy` files from the unzipped tree when needed.

### Files format (`export.format: files`)

Every selected file individually, plus `snapshot.json`:

```text
My Drive/
└── <drive_root>/                  e.g. ChatGPT/rot3k
    ├── snapshot.json              authoritative description, written last
    ├── AGENTS.md                  repository files at their relative paths
    ├── docs/
    │   └── design/MANIFEST.md
    └── …
```

### `snapshot.json` (format_version 1)

Complete snapshot:

```json
{
  "format_version": 1,
  "state": "complete",
  "generation_id": "20260928T160000Z-89f1da60677d-3fa2c1",
  "repository": "iascus/rt3k",
  "repository_key": "rot3k",
  "branch": "design-docs",
  "commit": "89f1da60677d…",
  "tree": "…",
  "generated_at": "2026-09-28T16:00:00Z",
  "previous_commit": "…",
  "drive_root": "ChatGPT/rot3k",
  "snapshot_file": "snapshot.json",
  "reader_notes": "…",
  "counts": {"listed": 1651, "exported": 1651, "bootstrap": 5, "index": 7, "lazy": 1639},
  "files": {
    "AGENTS.md": {
      "blob_sha": "…", "size": 5120, "class": "bootstrap",
      "exported": true, "mime_type": "text/markdown", "drive_file_id": "1AbC…"
    },
    "docs/characters/records-juan-23/x.md": {
      "blob_sha": "…", "size": 89321, "class": "lazy", "exported": true, "…": "…"
    },
    "assets/logo.bin": {
      "blob_sha": "…", "size": 2048, "class": "lazy", "exported": false, "reason": "binary"
    }
  }
}
```

| Field | Meaning |
|---|---|
| `state` | `complete`, or `updating` while an export is in progress (see below). |
| `commit` | The single Git commit every listed file comes from. |
| `generation_id` | Unique per export; changes even if the commit does not. |
| `files.<path>.blob_sha` | Git blob SHA of the file at `commit` (`git rev-parse <commit>:<path>`). |
| `files.<path>.class` | `bootstrap`: read at conversation start. `index`: catalogues, read at start. `lazy`: read only when needed. |
| `files.<path>.exported` | `false` when listed but not present in Drive; `reason` is `binary`, `too_large`, `symlink` or `unsupported_path`. |
| `files.<path>.drive_file_id` | Drive file ID; stable across exports while the path exists. |

Files matched by `exclude`, or by no rule at all, are not listed.

### Selection by the repository's manifest

With `export.manifest: docs/design/MANIFEST.md` the bridge reads that file
**at the exported commit** and exports exactly the paths in its
`project_source_files` block (between `<!-- PROJECT_SOURCE_FILES_BEGIN -->`
and `<!-- PROJECT_SOURCE_FILES_END -->`), minus configured `exclude`s. An
optional `project_source_materialization` block
(`<!-- PROJECT_SOURCE_MATERIALIZATION_BEGIN/END -->`) sets `default`
(`bootstrap`) and named lazy classes with `globs`/`paths`; a lazy file's
entry carries `"lazy_class": "<name>"`. A listed path absent from the commit
appears with `exported: false, reason: "missing"`. A malformed manifest, a
duplicate or unsafe path, or a path matching two lazy classes fails the
refresh. `snapshot.json` then includes
`"selection": {"source": "manifest", "manifest": "…", "manifest_blob_sha": "…"}`.

While an export runs, `snapshot.json` is replaced by a marker:

```json
{
  "format_version": 1,
  "state": "updating",
  "generation_id": "…",
  "repository": "iascus/rt3k",
  "repository_key": "rot3k",
  "branch": "design-docs",
  "previous_commit": "…",
  "target_commit": "…",
  "started_at": "2026-09-28T16:00:00Z",
  "reader_notes": "…"
}
```

**Reader rules**

1. Read `snapshot.json` first. If `state` is not `complete`, the files are in
   flux: wait and read it again (or ask the user to run *Refresh Git Snapshot*).
2. Read all `bootstrap` and `index` files.
3. Read `lazy` files only when the task needs them.
4. Use `commit` as `expected_base_sha` when proposing changes.

### Export order (files format)

1. Write `snapshot.json` with `state: updating`.
2. Upload new/changed files in place (unchanged blob SHA ⇒ skipped); move
   files that are no longer exported to the Drive trash.
3. Write the final `snapshot.json` with `state: complete` — always last.

A crash between 1 and 3 leaves the `updating` marker in place, which is
accurate. The next successful refresh completes it.

## Push: the publication artifact

```text
<repo>-publish.zip
├── request.json
└── changes.patch
```

Exactly these two members, at the top level, stored or deflated, not
encrypted. Anything else is rejected.

### `request.json` (format_version 1)

```json
{
  "format_version": 1,
  "repository": "iascus/rt3k",
  "branch": "design-docs",
  "expected_base_sha": "89f1da60677d0c0e5a1c6d2f8e4b3a2918273645",
  "patch_sha256": "5d41402abc4b2a76b9719d911017c592…",
  "commit_message": "Refine encounter activation UI",
  "created_at": "2026-09-28T16:00:00Z",
  "generator": "chatgpt"
}
```

| Field | Rules |
|---|---|
| `format_version` | Integer `1`. |
| `repository` | `owner/name`; must be the configured repository of the endpoint (or a declared former name). |
| `branch` | Must be in the repository's `allowed_branches`. |
| `expected_base_sha` | Full 40-character lowercase hex commit ID (64 for SHA-256 repositories). Abbreviations are rejected. |
| `patch_sha256` | SHA-256 of `changes.patch` bytes, hex. Detects corruption only. |
| `commit_message` | Non-blank, no NUL, at most 16 KiB. Used verbatim apart from trimming trailing whitespace. |
| `created_at`, `generator` | Optional, informational, never used for decisions. |

Unknown fields and duplicate keys are rejected.

### `changes.patch`

A `git diff`-format patch relative to `expected_base_sha`, applied with
`git apply --index` (paths `a/…` and `b/…`, strip level 1). Text changes only:

- adding, modifying, deleting and renaming regular files is allowed;
- symlinks, submodules, binary patches and paths matching `denied_paths`
  are rejected;
- whitespace errors in added lines fail `git diff --check`.

### Limits (defaults)

| Limit | Default |
|---|---|
| Upload (compressed) | 5 MiB |
| Total decompressed | 20 MiB |
| Per member | 16 MiB |
| Entries | 8 |
| `request.json` | 64 KiB |

### Responses

Every validate/publish response is one JSON object:

```json
{
  "ok": true,
  "operation": "publish",
  "repository": "iascus/rt3k",
  "branch": "design-docs",
  "expected_base_sha": "…",
  "observed_sha": "…",
  "old_sha": "…",
  "new_sha": "…",
  "changed_files": [{"path": "docs/x.md", "insertions": 3, "deletions": 1}],
  "files_changed": 1,
  "insertions": 3,
  "deletions": 1,
  "validation": {"passed": true, "checks": [{"name": "git diff --check", "passed": true, "exit_code": 0, "duration_ms": 12, "output_tail": ""}]},
  "git_publish": "success",
  "snapshot_refresh": "success",
  "snapshot_commit": "…",
  "snapshot_error": null,
  "error": null,
  "message": "Published 0a1b2c3d4e5f to iascus/rt3k@design-docs: 1 file(s), +3 -1. Drive snapshot updated."
}
```

`message` is a single human-readable line suitable for display.

| HTTP | `error.code` | Meaning |
|---|---|---|
| 200 | — | Success (`git_publish` may be `success` with `snapshot_refresh: failed`) |
| 400 | `invalid_artifact`, `patch_checksum_mismatch` | Archive or `request.json` invalid |
| 401 | `unauthorized` | Missing or wrong bearer token |
| 403 | `not_allowed` | Repository or branch not allowed for this endpoint |
| 404 | `unknown_repository` | No such repository key |
| 409 | `remote_changed` | Branch head ≠ `expected_base_sha`; regenerate against the new head |
| 409 | `push_race` | Branch moved between check and push; nothing was overwritten |
| 413 | `artifact_too_large` | Size limit exceeded |
| 422 | `patch_does_not_apply`, `patch_policy_violation`, `validation_failed` | Patch rejected; nothing committed |
| 502 | `push_rejected`, `snapshot_failed` | Remote refused the push / Drive export failed |

## Instructions for the AI assistant

Paste into the ChatGPT project instructions (adjust names):

> **Reading the repository.** The repository snapshot is the Google Drive file
> `ChatGPT/<repo>/<repo>-snapshot.zip`. Download it and unzip it with Python.
> Read `snapshot.json` inside it first: `commit` is the exact version you are
> looking at. Then read every file whose `class` is `bootstrap`. Read `lazy`
> files from the unzipped tree only when a task needs them. Never mix files
> from different ZIP downloads; if you fetch the ZIP again, re-read
> `snapshot.json`.
>
> **Proposing changes.** Never claim to have committed anything. When I ask you
> to publish, produce one file `<repo>-publish.zip` containing exactly:
> `changes.patch`, a unified `git diff` (paths prefixed `a/` and `b/`) against
> `commit` from `snapshot.json`, text files only, no trailing whitespace on
> added lines; and `request.json` with `format_version` 1, `repository`
> (from `snapshot.json`), `branch`, `expected_base_sha` (= `commit`),
> `patch_sha256` (SHA-256 hex of the exact `changes.patch` bytes), and
> `commit_message`. Build the ZIP with Python's `zipfile` (deflated, no
> directories, no other files), verify the checksum after writing, and give me
> the file to download. I publish it myself from the share sheet.

Example generator (what ChatGPT's Python tool should run):

```python
import hashlib, json, zipfile

patch = open("changes.patch", "rb").read()
request = {
    "format_version": 1,
    "repository": "iascus/rt3k",
    "branch": "design-docs",
    "expected_base_sha": "<commit from snapshot.json>",
    "patch_sha256": hashlib.sha256(patch).hexdigest(),
    "commit_message": "Refine encounter activation UI",
    "generator": "chatgpt",
}
with zipfile.ZipFile("rot3k-publish.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("request.json", json.dumps(request, indent=2))
    zf.writestr("changes.patch", patch)
```

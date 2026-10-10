# Security

agent-git-bridge lets an AI assistant *propose* Git changes and read exact
repository snapshots, while pushing to Git stays an explicit user action. This
document states what it protects, how, and what it does not.

## Assets

| Asset | Where it lives |
|---|---|
| GitHub write access (fine-grained PAT) | Bridge host only |
| Google Drive refresh token (`drive.file`) | Bridge host only |
| Bridge bearer token | Bridge host, and the user's iOS Shortcuts |
| Repository history | GitHub (authoritative) |

ChatGPT never receives any credential, local path or direct network access to
the bridge.

## Trust boundaries

```text
ChatGPT ──(data only: <key>-push.zip)──► user's share tap ──► iOS Shortcut
   ──HTTPS over Tailscale──► Tailscale Serve ──HTTP loopback──► bridge ──► GitHub
```

Everything inside the push ZIP is **untrusted input**: repository, branch,
base SHA, commit message and patch are validated against server
configuration, and the patch is applied only in an isolated worktree.

## Controls

**Network**

- Uvicorn binds to a loopback address; configuration refuses anything else
  and the serve command re-checks it.
- TLS and tailnet reachability are provided by Tailscale Serve. Funnel is not
  used; `tailscale serve status` must show `(tailnet only)`.
- No port forwarding; the service port refuses LAN and tailnet-IP
  connections (verified, see `docs/TAILSCALE_SETUP.md`).

**Authentication**

- Every endpoint except `/health` requires `Authorization: Bearer <token>`;
  comparison is constant-time; the token is at least 32 characters (generated
  as 48 random bytes); the service refuses to start without one.
- Authentication happens before repository lookup, so unauthenticated callers
  cannot probe repository names. `/health` returns only `{"status":"ok"}`.
- OpenAPI/Swagger endpoints are disabled.

**Authority**

- Only repositories and branches listed in configuration can be fetched,
  validated, committed or exported. The URL key and the repository named in
  `request.json` must agree. `request.json.branch` may name the default
  working branch, be omitted/`null` (same effect), or name one of
  `additional_working_branches`; any other value — including the
  integration branch or an arbitrary string — is refused before any Git
  operation, exactly like the single-branch case.
- No endpoint accepts Git arguments, shell commands, file paths, URLs or
  remote names. Git runs via fixed argument lists (`shell=False`) with values
  that are either server configuration or validated full-length hex SHAs.
- Pull requests: with `pull_request.base` configured, the bridge only lists
  open pull requests and opens one from the working branch into the
  integration branch. It never merges, closes, edits or comments. The token's
  *Pull requests* permission would technically allow merging through the API;
  the bridge contains no code path that does so.
- **One exception to "no force push"**: with `rebase_after_squash_merge`,
  Refresh may rewrite **a working branch** (the default one, or an
  additional one, independently of each other) after its own PR was
  squash-merged, using `--force-with-lease=<branch>:<exact old head>` (a
  compare-and-swap: anything pushed meanwhile is never overwritten), only
  after the rebased tree was verified equal to `git merge-tree` of the two
  heads. The integration branch can never be rewritten (enforced in code —
  the lease guard checks membership in the full set of configured working
  branches, never a single hard-coded name), and a push never triggers it.
  Rebasing one working branch never touches another's ref, pair or PR.
- Pushes use a plain refspec (`<sha>:refs/heads/<branch>`): no `+`, no
  `--force`, no `--force-with-lease`, no deletions, no tags, no ref rewriting.
- Optimistic concurrency: the branch head must equal `expected_base_sha`
  (409 otherwise) and Git's fast-forward rule rejects a push if the branch
  moved in the meantime (409 `push_race`). No automatic merge, rebase or
  retry.

**Patch handling**

- Archive: size, entry-count and decompressed-size limits enforced on actual
  bytes (ZIP bombs), exact member names only (path traversal), no duplicates,
  no encryption, strict JSON (no duplicate keys, no unknown fields).
- Applied with `git apply --index` in a fresh temporary worktree at the exact
  base commit; Git refuses paths outside the tree or inside `.git`.
- Rejected after applying: empty patches, symlinks, submodules, `denied_paths`
  (default `.github/**`). Binary add/modify/delete is accepted — Git's own
  binary-patch format (`GIT binary patch`, literal/base85) is applied by the
  same `git apply --index` call as text hunks, so a malformed binary patch
  fails the same way a malformed text patch does (`patch_does_not_apply`,
  nothing committed); it still cannot create a symlink or submodule, or touch
  a denied path, because that check runs on the resulting tree, not the
  patch's own claimed content type.
- The committed tree is captured before validation commands run, so a
  validator cannot alter what is committed.

**Git hygiene**

- System and global Git configuration are ignored (`GIT_CONFIG_NOSYSTEM`,
  `GIT_CONFIG_GLOBAL=/dev/null`); prompts are disabled; Git cannot discover a
  repository above its working directory.
- Extra Git settings are limited to an allowlist (`http.sslBackend`,
  `http.sslCAInfo`, `http.proxy`, `http.version`).
- The GitHub token is passed as an HTTP header through `GIT_CONFIG_*`
  environment variables, only to `fetch`/`push`; never on the command line,
  never written to Git config or disk by the bridge.
- The persistent clone is bare and never checked out; its remote URL must
  match configuration.

**Snapshots**

- Built from Git objects of a single commit, never a working tree.
- Each refresh builds the integration snapshot and the working overlay from
  one resolved pair of commits; the snapshot pins the overlay's SHA-256, so a
  reader can never accept artifacts from different generations.
- Only the integration branch and the configured working branch(es) are
  ever fetched; pushes can only target a working branch, never the
  integration branch. An additional working branch is fetched with its own,
  single-refspec fetch, so a repository's refresh never fails outright just
  because one additional branch does not exist yet — a required branch
  (integration, or the default working branch) missing is still a hard
  failure, as before.
- Drive scope `drive.file`: the bridge cannot read or change any file it did
  not create. Removed files go to the Drive trash (recoverable). An
  additional working branch's pair uses its own Drive property value
  (`gb_kind: "<kind>@<branch>"`), so it can never be found by, or
  overwritten through, the default pair's own lookup query, and vice versa.
- Only text files listed in `PROJECT_SOURCE_FILES` are exported that way;
  binaries, symlinks and oversized files are listed but not uploaded.
- A repository may opt a directory into binary export via its own manifest's
  `PROJECT_ARTIFACT_ROOTS` (never a bridge-side setting). Each root is
  validated against the same traversal/absolute-path check as every listed
  source path (rejecting `..`, absolute paths); only tracked Git blobs under
  it are read (`git ls-tree`, never the filesystem, so no symlink can be
  followed to escape the repository); symlinks and oversized files under a
  root are still excluded, the same as for listed source files.

**Logging**

- Structured events record repository, branch, SHAs, changed paths,
  validation exit codes and outcomes. They never contain tokens,
  `Authorization` headers, patch contents, file contents or commit messages.
- Optional append-only JSON Lines audit log (`audit_log`).

## Known risks and limitations

1. **The user cannot realistically review every patch.** The share tap
   authorises *publication*, not the correctness of the content. Mitigations:
   narrow branch allowlist (e.g. a `design-docs` branch, not `main`), the
   `validate-patch` endpoint, `denied_paths`, GitHub history for rollback.
2. **Validation commands execute on patched content.** A configured
   validator that runs repository code (tests, builds, `npm` scripts) gives
   the patch author code execution as the service user, who can read the
   token files. The environment is scrubbed, but files are not hidden. Prefer
   validators that only parse files, or run under the systemd sandbox with a
   dedicated user. The default configuration runs none.
3. **Rewind race.** If another writer force-pushes the branch *backwards* to
   an ancestor of `expected_base_sha` between the bridge's fetch and push, a
   normal fast-forward push succeeds and re-pushes the removed commits.
   Closing this requires `--force-with-lease`, which the design forbids.
4. **Bearer token on the phone.** Shortcuts sync through iCloud; the token is
   as safe as your Apple account. Rotate with `git-bridge init-token --force`
   if a device is lost. The token alone is useless outside the tailnet.
5. **`patch_sha256` is not a signature.** It only detects corruption.
6. **Windows host.** The scheduled task runs as your user account, so the
   bridge (and any validator) has your user's file access. The Linux systemd
   unit is the more isolated option.
7. **Certificate Transparency.** The `*.ts.net` hostname appears in public CT
   logs; choose a non-sensitive machine name.

## Rotating and revoking

| Credential | Rotate / revoke |
|---|---|
| Bearer token | `git-bridge init-token --force`, update Shortcuts |
| GitHub PAT | Generate a new one, replace the file; revoke the old one on GitHub |
| Google | <https://myaccount.google.com/permissions> → Git Bridge → remove; delete `google-token.json`; `git-bridge google-login` to re-authorise |
| Network access | `tailscale serve --https=10000 off` |

## Reporting

Open an issue at <https://github.com/iascus/agent-git-bridge/issues>
without including secrets, or contact the maintainer privately through GitHub.

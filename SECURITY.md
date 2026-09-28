# Security

agent-git-bridge lets an AI assistant *propose* Git changes and read exact
repository snapshots, while publication stays an explicit user action. This
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
ChatGPT ──(data only: publish.zip)──► user's share tap ──► iOS Shortcut
   ──HTTPS over Tailscale──► Tailscale Serve ──HTTP loopback──► bridge ──► GitHub
```

Everything inside `publish.zip` is **untrusted input**: repository, branch,
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
  `request.json` must agree.
- No endpoint accepts Git arguments, shell commands, file paths, URLs or
  remote names. Git runs via fixed argument lists (`shell=False`) with values
  that are either server configuration or validated full-length hex SHAs.
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
- Rejected after applying: empty patches, symlinks, submodules, binary
  changes, `denied_paths` (default `.github/**`).
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
- `snapshot.json` is marked `updating` before files change and written
  `complete` last, so readers can detect in-flux states.
- Drive scope `drive.file`: the bridge cannot read or change any file it did
  not create. Removed files go to the Drive trash (recoverable).
- Only text files are exported; binaries, symlinks and oversized files are
  listed but not uploaded.

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
   normal fast-forward push succeeds and re-publishes the removed commits.
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

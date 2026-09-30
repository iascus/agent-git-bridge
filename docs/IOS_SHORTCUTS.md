# iOS Shortcuts

Two Shortcuts connect the iPhone to the bridge. Neither ever sees GitHub or
Google credentials; each only holds the bridge's bearer token.

Before you start:

- Tailscale is installed on the iPhone, signed in to the same tailnet, and
  **on**.
- `https://cave.tail364856.ts.net:10000/health` opens in Safari and shows
  `{"status":"ok"}`.
- You have the bearer token from the bridge host
  (`%USERPROFILE%\.config\agent-git-bridge\bearer-token.txt` on Windows,
  `/etc/git-bridge/bearer-token` on Linux). Transfer it privately, e.g. via a
  password manager or by typing it; do not paste it into a chat.

Replace `cave.tail364856.ts.net:10000` and `rot3k` below with your host and
repository key.

## Push Git Patch

Receives the push ZIP (`<key>-push.zip`) from ChatGPT's share sheet and POSTs
it unchanged. It advances the project's **working branch** (e.g.
`design-docs`); it never touches the integration branch (`main`).

1. **Shortcuts** app → **+** → name it **Push Git Patch**.
2. Tap the **ⓘ** (Details) → enable **Show in Share Sheet**. Tap *Share Sheet
   Types* and select only **Files**. Done.
3. At the top, the input block reads *Receive **Files** from **Share Sheet***.
   Set "If there's no input" to **Stop and Respond** (or *Ask For Files*).
4. Add action **Text** and paste the bearer token. Rename the result
   "Token" (long-press → Rename), optional.
5. Add action **Get Contents of URL**:
   - URL: `https://cave.tail364856.ts.net:10000/repos/rot3k/push`
   - Tap **Show More**:
     - Method: **POST**
     - Headers → **Add new header**:
       - Key `Authorization`, Value `Bearer ` followed by the **Text** variable
         (type "Bearer ", a space, then insert the Text magic variable).
       - Key `Content-Type`, Value `application/zip`
     - Request Body: **File** → select **Shortcut Input**.
6. Add action **Get Dictionary Value**: Get **Value** for key `message` in
   *Contents of URL*.
7. Add action **Get Dictionary Value**: Get **Value** for key `new_sha` in
   *Contents of URL*.
8. Add action **Show Result** (or *Show Alert*) with text:

   ```text
   <Dictionary Value (message)>
   Commit: <Dictionary Value (new_sha)>
   ```

That is the whole Shortcut. It must **not** unzip, parse, re-compress,
base64-encode or otherwise touch the file, and it contains no GitHub or Google
credentials.

The response `message` already reads e.g.:

- `Pushed 0a1b2c3d4e5f to iascus/rt3k@design-docs: 2 file(s), +10 -3. Opened PR #12 into main: https://github.com/iascus/rt3k/pull/12 Drive snapshot updated.`
- `Rejected (remote_changed): remote branch has moved; regenerate the patch against the current head`

and `new_sha` is empty on rejection.

**Migrating an existing "Publish Git Patch" Shortcut:** change the URL's last
segment from `/publish` to `/push` (the old path still works for now but is
deprecated), and rename the Shortcut to **Push Git Patch**.

### One Shortcut per repository, or a menu

For a second repository (e.g. `cyberpunk-tactics`), either duplicate the
Shortcut and change the URL, or insert **Choose from Menu** before step 5 with
one item per repository, each setting a **Text** variable `Repo` used in the
URL: `…:10000/repos/<Repo>/push`. The bridge also cross-checks the
repository named inside `request.json`, so sending a ZIP to the wrong
repository is rejected, not applied.

### Optional: dry run

Duplicate the Shortcut as **Validate Git Patch** with URL
`…/repos/rot3k/validate-patch`. It runs every check without committing.

## Refresh Git Snapshot

Re-exports both source states to Google Drive: the integration-branch
snapshot and the integration → working overlay. Use it after anything changed
the branches outside the bridge (a PR merged into `main`, a push or rebase
from VS Code), or after a push reported `snapshot_refresh: failed`.

1. New Shortcut → **Refresh Git Snapshot**. It does not need the Share Sheet.
2. **Text**: the bearer token.
3. **Get Contents of URL**:
   - URL: `https://cave.tail364856.ts.net:10000/repos/rot3k/refresh`
   - Method **POST**, header `Authorization: Bearer <Text>`, no body.
4. **Get Dictionary Value** for each of: `ok`, `repository`,
   `integration_branch`, `integration_commit`, `working_branch`,
   `working_commit`, `file_count`, `message`.
5. **Show Result**:

   ```text
   <ok> — <repository>
   <integration_branch>: <integration_commit>
   <working_branch>: <working_commit>
   Snapshot files: <file_count>
   <message>
   ```

A refresh uploads one ZIP and one diff (seconds for a few MB) and is skipped
entirely when neither branch changed.

## End-to-end test

1. In ChatGPT, ask for a trivial change (e.g. fix a typo in a Markdown file)
   and to "produce the push zip".
2. Share the ZIP → **Push Git Patch**. Expect `Pushed …` with a commit SHA.
3. Confirm the commit on GitHub (`design-docs` branch, `main` unchanged) and
   that in Drive `<key>-snapshot.zip`'s `snapshot.json` shows the new
   `working_commit` and `<key>-working.diff` contains the change.
4. Share the **same** ZIP again. Expect
   `Rejected (remote_changed): …` — the optimistic-concurrency check.
5. Turn Tailscale off on the phone and run the Shortcut again. It must fail to
   connect.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "The network connection was lost" (nothing in the bridge log) | A header is malformed, so Tailscale Serve rejects the HTTP/2 request before it reaches the bridge. Check that the header names are exactly `Authorization` and `Content-Type` (no spaces), and that the token in the **Text** action has no line break after it (tap at the end and delete any empty line). |
| "Could not connect to the server" | Tailscale off on phone, bridge not running, or Serve entry missing (`tailscale serve status`). |
| `Rejected (unauthorized)` | Token wrong/missing, or header not exactly `Bearer <token>`. |
| `Rejected (invalid_artifact)` | The Shortcut altered the file (check Request Body is **File** → Shortcut Input), or ChatGPT produced a malformed ZIP. |
| `Rejected (remote_changed)` | The working branch moved since ChatGPT read the snapshot. Refresh, and ask ChatGPT to regenerate against the new `working_commit`. |
| `Rejected (validation_failed)` | Look at `validation.checks` in the full response (e.g. trailing whitespace). |
| Pushed, but "Drive snapshot refresh FAILED" | The commit is on GitHub. Run **Refresh Git Snapshot**; if it keeps failing, see `git-bridge check` on the host (Google login may need renewing). |

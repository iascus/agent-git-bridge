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

## Publish Git Patch

Receives the ZIP from ChatGPT's share sheet and POSTs it unchanged.

1. **Shortcuts** app → **+** → name it **Publish Git Patch**.
2. Tap the **ⓘ** (Details) → enable **Show in Share Sheet**. Tap *Share Sheet
   Types* and select only **Files**. Done.
3. At the top, the input block reads *Receive **Files** from **Share Sheet***.
   Set "If there's no input" to **Stop and Respond** (or *Ask For Files*).
4. Add action **Text** and paste the bearer token. Rename the result
   "Token" (long-press → Rename), optional.
5. Add action **Get Contents of URL**:
   - URL: `https://cave.tail364856.ts.net:10000/repos/rot3k/publish`
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

- `Published 0a1b2c3d4e5f to iascus/rt3k@design-docs: 2 file(s), +10 -3. Drive snapshot updated.`
- `Rejected (remote_changed): remote branch has moved; regenerate the patch against the current head`

and `new_sha` is empty on rejection.

### One Shortcut per repository, or a menu

For a second repository (e.g. `cyberpunk-tactics`), either duplicate the
Shortcut and change the URL, or insert **Choose from Menu** before step 5 with
one item per repository, each setting a **Text** variable `Repo` used in the
URL: `…:10000/repos/<Repo>/publish`. The bridge also cross-checks the
repository named inside `request.json`, so sending a ZIP to the wrong
repository is rejected, not applied.

### Optional: dry run

Duplicate the Shortcut as **Validate Git Patch** with URL
`…/repos/rot3k/validate-patch`. It runs every check without committing.

## Refresh Git Snapshot

Re-exports the current branch head to Google Drive (e.g. after pushing from
elsewhere, or after a publication reported `snapshot_refresh: failed`).

1. New Shortcut → **Refresh Git Snapshot**. It does not need the Share Sheet.
2. **Text**: the bearer token.
3. **Get Contents of URL**:
   - URL: `https://cave.tail364856.ts.net:10000/repos/rot3k/refresh`
   - Method **POST**, header `Authorization: Bearer <Text>`, no body.
4. **Get Dictionary Value** for each of: `repository`, `branch`, `commit`,
   `exported`, `ok`, `message`.
5. **Show Result**:

   ```text
   <ok> — <repository>@<branch>
   Snapshot commit: <commit>
   Exported files: <exported>
   <message>
   ```

The first export of a large repository can take several minutes; run it once
from the host (`git-bridge refresh rot3k`) so the Shortcut only ever does
incremental updates. If the Shortcut times out, the export continues on the
server; run it again (or check `…/status`) to see the result.

## End-to-end test

1. In ChatGPT, ask for a trivial change (e.g. fix a typo in a Markdown file)
   and to "produce the publish zip".
2. Share the ZIP → **Publish Git Patch**. Expect `Published …` with a commit
   SHA.
3. Confirm the commit on GitHub (`design-docs` branch) and that
   `ChatGPT/<repo>/snapshot.json` in Drive shows the new `commit`.
4. Share the **same** ZIP again. Expect
   `Rejected (remote_changed): …` — the optimistic-concurrency check.
5. Turn Tailscale off on the phone and run the Shortcut again. It must fail to
   connect.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Could not connect to the server" | Tailscale off on phone, bridge not running, or Serve entry missing (`tailscale serve status`). |
| `Rejected (unauthorized)` | Token wrong/missing, or header not exactly `Bearer <token>`. |
| `Rejected (invalid_artifact)` | The Shortcut altered the file (check Request Body is **File** → Shortcut Input), or ChatGPT produced a malformed ZIP. |
| `Rejected (remote_changed)` | Someone pushed since ChatGPT read the snapshot. Refresh the snapshot and ask ChatGPT to regenerate against the new `commit`. |
| `Rejected (validation_failed)` | Look at `validation.checks` in the full response (e.g. trailing whitespace). |
| Published, but "Drive snapshot refresh FAILED" | The commit is on GitHub. Run **Refresh Git Snapshot**; if it keeps failing, see `git-bridge check` on the host (Google login may need renewing). |

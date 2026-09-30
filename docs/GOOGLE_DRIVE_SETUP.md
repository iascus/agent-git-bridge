# Google Drive setup

The bridge writes snapshots into **your own** Google Drive using OAuth for an
installed ("Desktop") application with the narrowest practical scope:

```text
https://www.googleapis.com/auth/drive.file
```

`drive.file` lets the bridge see and change **only files and folders it
created itself**. It cannot list, read or modify anything else in your Drive.
Consequence: let the bridge create its folders (`ChatGPT/<repo>`); a folder
you create manually with the same name is invisible to it and would result in
a second folder.

Everything below is free (no billing account needed).

## 1. Google Cloud project and API

1. <https://console.cloud.google.com/> → project selector → **New project**
   (e.g. `git-bridge`) → select it.
2. **☰ → APIs & Services → Library** → **Google Drive API** → **Enable**
   (direct link: `https://console.cloud.google.com/apis/library/drive.googleapis.com`).

## 2. OAuth consent screen (Google Auth Platform)

`https://console.cloud.google.com/auth/overview` → **Get started**:

- App name `Git Bridge`, user support email, **Audience: External**, contact
  email → Create.
- **Branding**: application home page
  `https://github.com/iascus/agent-git-bridge`, privacy policy
  `https://github.com/iascus/agent-git-bridge/blob/main/PRIVACY.md`. Leave the
  logo empty (a logo triggers brand verification).
- **Data Access** (optional): add `…/auth/drive.file`.
- **Audience → Publish app → Confirm** so the status is **In production**.

Why production: for External apps in *Testing* status Google expires refresh
tokens after **7 days**, which would silently break snapshot refreshes every
week. `drive.file` is a non-sensitive scope, so production needs no Google
review; you will see an "unverified app" screen once at login.

## 3. OAuth client

**Clients → Create client** → type **Desktop app** → name `git-bridge` →
**Create** → **Download JSON**.

Store it outside any repository:

| Host | Path |
|---|---|
| Windows | `%USERPROFILE%\.config\agent-git-bridge\google-client.json` |
| Linux | `/etc/git-bridge/google-client.json` (owner `git-bridge`, mode 0600) |

and reference it in the configuration:

```yaml
snapshot:
  transport: google_drive
google:
  client_secrets_file: …/google-client.json
  token_file: …/google-token.json
```

## 4. One-time login

On the bridge host, with a browser available:

```sh
git-bridge google-login
```

A browser opens: choose your account → *Advanced → Go to Git Bridge
(unsafe)* → allow "See, edit, create and delete only the specific Google Drive
files you use with this app". The bridge stores a refresh token in
`token_file` (mode 0600 on Linux).

Headless Linux host: run the login on a machine with a browser using the same
client JSON and configuration, then copy `google-token.json` to the host's
`token_file` over SSH.

Verify:

```sh
git-bridge check           # "[ok] Google login"
git-bridge refresh rot3k   # first full export
```

## Token refresh

Access tokens last an hour and are refreshed automatically with the stored
refresh token; the refreshed token is written back to `token_file`. The
refresh token itself does not expire for a production-status app unless it
is revoked, unused for six months, or your Google account password changes
with certain security settings. When it stops working, refresh and push
responses report `snapshot_refresh: failed` with "Google login expired or was
revoked", and `git-bridge check` shows `[FAIL] Google login`. Fix with
`git-bridge google-login`. Git publication is never affected.

## Where credentials live

| File | Contains | Sensitivity |
|---|---|---|
| `google-client.json` | OAuth client ID and client secret | Low for Desktop clients, but keep private |
| `google-token.json` | Refresh token for **your** Drive (scope `drive.file`) | Secret: grants access to bridge-created files |

Neither is ever sent to ChatGPT, the iPhone, logs or the audit log.

## Revoking access

1. <https://myaccount.google.com/permissions> → **Git Bridge** → **Delete all
   connections** / *Remove access*. The refresh token stops working
   immediately.
2. Delete `google-token.json` on the host.
3. Optionally delete the OAuth client (Cloud Console → Clients) or the whole
   project.

Exported files stay in your Drive until you delete them.

## What ChatGPT sees

ChatGPT reads the exported files through **its own** Google Drive
integration, which you connect in ChatGPT's settings. That is a separate grant
from you to OpenAI; the bridge is not involved in it and never talks to
ChatGPT.

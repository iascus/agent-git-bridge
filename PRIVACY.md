# Privacy Policy

*Last updated: 28 September 2026*

Git Bridge (`agent-git-bridge`) is a personal, self-hosted tool operated by
its author for their own use. It is not offered to the public, has no
user accounts, and is not a commercial service.

## What the app does

Git Bridge copies files from the operator's own GitHub repositories into the
operator's own Google Drive, and publishes Git changes that the operator
explicitly approves back to those repositories.

## Google user data accessed

The app requests a single Google OAuth scope:

- `https://www.googleapis.com/auth/drive.file` — access only to Google Drive
  files and folders that Git Bridge itself created.

With this scope the app **cannot** see, read, list or modify any other files
in the Google Drive account. It does not request access to email, contacts,
calendar, profile information or any other Google service.

## How the data is used

- The app creates, updates and deletes the files and folders it exported
  (copies of repository files and a `snapshot.json` manifest) so that they
  match a specific Git commit.
- Data is used solely to provide this export. It is not used for
  advertising, profiling, analytics, or training machine-learning models,
  and it is not sold.

Git Bridge's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Storage and sharing

- Exported files are stored in the operator's own Google Drive.
- OAuth tokens and GitHub credentials are stored only on the operator's own
  server, outside of source control, and are never sent to any third party.
- The app does not share Google user data with anyone. The operator may
  separately choose to let other tools they use (for example an AI assistant
  with its own Google Drive integration) read those Drive files; that access
  is granted by the operator directly to that tool, not by Git Bridge.
- Operational logs record repository names, commit identifiers, file paths
  and outcomes. They do not record file contents or credentials.

## Retention and deletion

- Exported files remain in Google Drive until the operator deletes them or
  the app replaces them with a newer export.
- Access can be revoked at any time at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
  Revoking access immediately invalidates the stored tokens; the operator
  then deletes the local token file.

## Contact

Questions about this policy can be raised as an issue at
<https://github.com/iascus/agent-git-bridge/issues>.

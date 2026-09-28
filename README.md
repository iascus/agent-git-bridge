# agent-git-bridge

A small, security-conscious bridge between an AI conversation and Git:

- **Pull.** Exports an exact Git commit of configured repositories to Google
  Drive, file by file, with a `snapshot.json` manifest. ChatGPT reads it
  through its normal Drive integration.
- **Push.** ChatGPT produces a `publish.zip` (`request.json` +
  `changes.patch`). You share it from the iOS Share Sheet to a Shortcut, which
  POSTs it over private Tailscale HTTPS. The bridge verifies the expected base
  commit, applies and validates the patch in an isolated worktree, commits,
  and pushes without force.

```text
GitHub ──► bridge ──► Google Drive ──► ChatGPT
ChatGPT ──► publish.zip ──► iOS Share Sheet ──► Tailscale HTTPS ──► bridge ──► GitHub
```

The bridge holds the GitHub and Google credentials; ChatGPT and the phone
never do. Only configured repositories and branches can be touched, and there
is no generic Git, shell or file endpoint.

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Design, diagrams, trust boundary, deviations from the original spec |
| [SECURITY.md](SECURITY.md) | Controls, known risks, credential rotation |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | `snapshot.json` and `publish.zip` formats, responses, instructions for ChatGPT |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Windows scheduled task, Linux systemd, secrets, updates, recovery |
| [docs/TAILSCALE_SETUP.md](docs/TAILSCALE_SETUP.md) | Serve setup and exposure verification |
| [docs/GOOGLE_DRIVE_SETUP.md](docs/GOOGLE_DRIVE_SETUP.md) | Cloud project, OAuth, login, revocation |
| [docs/IOS_SHORTCUTS.md](docs/IOS_SHORTCUTS.md) | *Publish Git Patch* and *Refresh Git Snapshot* |
| [PRIVACY.md](PRIVACY.md) | Privacy policy for the Google OAuth app |

## Quick start

Requires Python 3.11+, Git 2.32+, and Tailscale for remote access.

```sh
python -m venv .venv
. .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                                # no network needed

cp config/example.yaml ~/.config/agent-git-bridge/config.yaml   # then edit
git-bridge init-token                 # bearer token for the Shortcuts
git-bridge google-login               # once, in a browser
git-bridge check                      # verifies config, tokens, remotes
git-bridge refresh rot3k              # first Drive export
git-bridge serve                      # http://127.0.0.1:8000
tailscale serve --bg --https=10000 http://127.0.0.1:8000
```

### Commands

| Command | Purpose |
|---|---|
| `git-bridge serve` | Run the HTTP service (loopback only, one worker) |
| `git-bridge check` | Diagnose configuration and credentials |
| `git-bridge status <repo>` | Remote heads and snapshot state |
| `git-bridge refresh <repo>` | Export the current branch head |
| `git-bridge validate <repo> <zip>` | Run all publication checks locally |
| `git-bridge publish <repo> <zip>` | Publish a ZIP from the host |
| `git-bridge init-token [--force]` | Create or rotate the bearer token |
| `git-bridge google-login` | One-time Google consent |

### HTTP API

| Method | Path | Auth |
|---|---|---|
| GET | `/health` | none |
| GET | `/repos/{repo}/status` | bearer |
| POST | `/repos/{repo}/refresh` | bearer |
| POST | `/repos/{repo}/validate-patch` | bearer, body = ZIP |
| POST | `/repos/{repo}/publish` | bearer, body = ZIP |

## Configuration

Start from [config/example.yaml](config/example.yaml) (Linux) or
[config/example.windows.yaml](config/example.windows.yaml). Several
repositories can be configured side by side (e.g. `rot3k` and
`cyberpunk-tactics`); each gets its own clone, lock and Drive folder, and
GitHub renames are handled with `former_github_repos`. Secrets (GitHub token,
bearer token, Google credentials) live in files outside the repository,
referenced by path.

## Status

All phases (Git core, HTTP service, snapshots, Google Drive, Tailscale
deployment, iOS Shortcuts) are implemented and covered by tests using local
bare repositories and a fake Drive.

## License

GPL-3.0. See [LICENSE](LICENSE).

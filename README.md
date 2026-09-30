# agent-git-bridge

A small, security-conscious bridge between an AI conversation and Git.

Each project has an **integration branch** (e.g. `main`) and a **working
branch** (e.g. `design-docs`):

- **Pull.** On every refresh the bridge writes two files with stable names to
  Google Drive, built from one resolved pair of branch heads:
  - `<key>-snapshot.zip`: the complete project source set (as listed by the
    repository's own manifest) of the integration branch, plus
    `snapshot.json` describing both branch states;
  - `<key>-working.diff`: the exact tree diff integration → working branch.
  Applying the diff to the unzipped snapshot reproduces the working branch.
  ChatGPT reads both through its normal Drive integration, without GitHub.
- **Push.** ChatGPT produces `<key>-push.zip` (`request.json` +
  `changes.patch`, working tree → its HEAD). You share it from the iOS Share
  Sheet to a Shortcut, which POSTs it over private Tailscale HTTPS. The
  bridge checks the working branch head against `expected_base_sha`, applies
  and validates the patch in an isolated worktree, commits, pushes to the
  working branch without force, optionally opens a PR into the integration
  branch, and refreshes Drive.
- **Integration** happens through that PR, merged by you. The bridge never
  writes to the integration branch, merges or rebases.

```text
main ───────────────► complete source snapshot
 │
 └── tree diff ─────► working overlay ◄──── design-docs

ChatGPT ──► <key>-push.zip ──► iOS Share Sheet ──► Tailscale HTTPS ──► bridge ──► design-docs ──PR──► main
```

The bridge holds the GitHub and Google credentials; ChatGPT and the phone
never do. Only configured repositories and branches can be touched, and there
is no generic Git, shell or file endpoint.

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Branch roles, source states, generations, diagrams, trust boundary |
| [SECURITY.md](SECURITY.md) | Controls, known risks, credential rotation |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | `snapshot.json` v2, overlay, push ZIP, responses, instructions for ChatGPT, migration |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Windows scheduled task, Linux systemd, secrets, updates, recovery |
| [docs/TAILSCALE_SETUP.md](docs/TAILSCALE_SETUP.md) | Serve setup and exposure verification |
| [docs/GOOGLE_DRIVE_SETUP.md](docs/GOOGLE_DRIVE_SETUP.md) | Cloud project, OAuth, login, revocation |
| [docs/IOS_SHORTCUTS.md](docs/IOS_SHORTCUTS.md) | *Push Git Patch* and *Refresh Git Snapshot* |
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
git-bridge check                      # verifies config, tokens, both branches, PR access
git-bridge refresh rot3k              # first Drive export
git-bridge serve                      # http://127.0.0.1:8000
tailscale serve --bg --https=10000 http://127.0.0.1:8000
```

### Commands

| Command | Purpose |
|---|---|
| `git-bridge serve` | Run the HTTP service (loopback only, one worker; stops earlier instances) |
| `git-bridge check` | Diagnose configuration and credentials |
| `git-bridge status <repo>` | Both branch heads, merge base, ahead/behind, current generation |
| `git-bridge refresh <repo>` | Export snapshot + working overlay |
| `git-bridge validate <repo> <zip>` | Run all push checks locally |
| `git-bridge push <repo> <zip>` | Push a ZIP from the host |
| `git-bridge init-token [--force]` | Create or rotate the bearer token |
| `git-bridge google-login` | One-time Google consent |

### HTTP API

| Method | Path | Auth |
|---|---|---|
| GET | `/health` | none |
| GET | `/repos/{repo}/status` | bearer |
| POST | `/repos/{repo}/refresh` | bearer |
| POST | `/repos/{repo}/validate-patch` | bearer, body = ZIP |
| POST | `/repos/{repo}/push` | bearer, body = ZIP (`/publish`: deprecated alias) |

## Configuration

Start from [config/example.yaml](config/example.yaml) (Linux) or
[config/example.windows.yaml](config/example.windows.yaml). Per repository:
`github_repo`, `integration_branch`, `working_branch`, `export.manifest`,
`export.drive_root`, optional `pull_request`. Several repositories can be
configured side by side under stable project keys (e.g. `rot3k`,
`cyberpunk-tactics`), each with its own clone, lock and Drive folder; GitHub
renames are handled with `former_github_repos`. Secrets live in files outside
the repository, referenced by path.

## License

GPL-3.0. See [LICENSE](LICENSE).

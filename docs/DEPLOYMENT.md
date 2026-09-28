# Deployment

The bridge is one Python process listening on `127.0.0.1:8000`, exposed to
your tailnet by Tailscale Serve. Two supported setups:

- **Windows** (reference deployment): per-user scheduled task.
- **Linux**: hardened systemd unit.

Docker is not provided. If you containerise it, publish the port as
`127.0.0.1:8000:8000` only, never `8000:8000`.

Requirements: Python 3.11+, Git 2.32+, Tailscale 1.52+.

## Local development

```sh
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

A development configuration can use `snapshot.transport: local` with
`local_root: ./var/export` to write snapshots to a folder instead of Drive,
and `remote_url:` pointing at a local bare repository.

```sh
git-bridge --config config/local.yaml check
git-bridge --config config/local.yaml serve
```

## Configuration and secrets

| Item | Windows | Linux |
|---|---|---|
| Configuration | `%USERPROFILE%\.config\agent-git-bridge\config.yaml` | `/etc/git-bridge/config.yaml` |
| GitHub token | `…\github-token.txt` | `/etc/git-bridge/github-token` |
| Bearer token | `…\bearer-token.txt` | `/etc/git-bridge/bearer-token` |
| Google client | `…\google-client.json` | `/etc/git-bridge/google-client.json` |
| Google token | `…\google-token.json` | `/var/lib/git-bridge/google-token.json` |
| Clones, work dir, audit log | `%LOCALAPPDATA%\agent-git-bridge\` | `/var/lib/git-bridge/` |

Start from [config/example.windows.yaml](../config/example.windows.yaml) or
[config/example.yaml](../config/example.yaml). `GIT_BRIDGE_CONFIG` or
`--config` selects the file.

### GitHub token

GitHub → *Settings → Developer settings → Fine-grained tokens → Generate*:

- Resource owner: your account. Repository access: **only** the configured
  repositories.
- Permissions: **Contents: Read and write** (Metadata read-only is implied).
  Do **not** grant *Workflows* (prevents changes to `.github/workflows`), or
  administration permissions.
- Set an expiry and a reminder to rotate it: replace the file, no restart
  needed (it is read on each fetch/push).

A GitHub App installation token works as well (same header mechanism) but
needs a helper to renew it hourly; not included.

### Bearer token

```sh
git-bridge init-token          # writes server.bearer_token_file (48 random bytes, URL-safe)
git-bridge init-token --force  # rotate; then update the iOS Shortcuts
```

### Check everything

```sh
git-bridge check
```

reports Git version, loopback binding, tokens, Google login and a fetch of
every allowed branch, without changing anything.

## Windows (scheduled task)

```powershell
cd E:\GitHub\agent-git-bridge
py -3 -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\git-bridge check
.venv\Scripts\git-bridge refresh rot3k            # first export, interactive
powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1
```

The task runs `pythonw -m git_bridge serve` hidden at logon under your
account (no administrator rights), restarts it on failure, and logs to
`%LOCALAPPDATA%\agent-git-bridge\service.log`. It only runs while you are
logged in; the PC must be on and logged in to publish from the phone.

Operate:

```powershell
Get-ScheduledTask agent-git-bridge          # State: Running
Stop-ScheduledTask agent-git-bridge
Start-ScheduledTask agent-git-bridge        # after a code or config change
deploy\windows\uninstall-task.ps1           # remove
Get-Content $env:LOCALAPPDATA\agent-git-bridge\service.log -Tail 50
```

Use `git_extra_config: [http.sslBackend=schannel]` on Windows so HTTPS to
GitHub uses the Windows certificate store (the bridge deliberately ignores the
system Git configuration).

## Linux (systemd)

```sh
sudo useradd --system --home /var/lib/git-bridge --shell /usr/sbin/nologin git-bridge
sudo git clone https://github.com/iascus/agent-git-bridge /opt/agent-git-bridge
sudo python3 -m venv /opt/agent-git-bridge/.venv
sudo /opt/agent-git-bridge/.venv/bin/pip install /opt/agent-git-bridge
sudo install -d -o root -g git-bridge -m 0750 /etc/git-bridge
sudo install -o root -g git-bridge -m 0640 config/example.yaml /etc/git-bridge/config.yaml   # then edit
# put github-token, google-client.json in /etc/git-bridge (group git-bridge, mode 0640)
sudo GIT_BRIDGE_CONFIG=/etc/git-bridge/config.yaml /opt/agent-git-bridge/.venv/bin/git-bridge init-token
sudo chgrp git-bridge /etc/git-bridge/bearer-token && sudo chmod 0640 /etc/git-bridge/bearer-token
sudo cp /opt/agent-git-bridge/deploy/systemd/git-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now git-bridge
sudo systemctl status git-bridge
journalctl -u git-bridge -f
```

The unit runs as the unprivileged `git-bridge` user with a read-only system,
no home access, private `/tmp`, no capabilities, and write access only to
`/var/lib/git-bridge`.

Then set up Tailscale Serve: [TAILSCALE_SETUP.md](TAILSCALE_SETUP.md).

## Updating

```sh
git pull
pip install -e .            # or: pip install . on Linux
# Windows: Stop-ScheduledTask / Start-ScheduledTask agent-git-bridge
# Linux:   sudo systemctl restart git-bridge
```

## Recovery

- **Clone corrupted or wrong**: stop the service, delete the repository's
  `local_path`, start again. It is a cache and is recreated on demand.
- **Stale worktrees** after a crash: cleaned automatically on the next
  publication (`git worktree prune`); the `work_dir` can be emptied while the
  service is stopped.
- **Drive snapshot stuck in `updating`**: run `git-bridge refresh <repo>` or
  the *Refresh Git Snapshot* Shortcut.
- **Drive folder deleted by accident**: files are in the Drive trash; or just
  refresh, which recreates them.

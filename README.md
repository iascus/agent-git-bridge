# agent-git-bridge

A small, security-conscious bridge that lets an AI conversation read exact Git
snapshots via Google Drive and propose commits as a `publish.zip`, which the
user explicitly publishes from the iOS Share Sheet over private Tailscale HTTPS.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design, trust boundary and
deviations from the original specification.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Git core: config, fetch/status, isolated worktree, validate, commit, guarded push | implemented |
| 2 | HTTP service (FastAPI, bearer auth, loopback only) | planned |
| 3 | Snapshot generation | planned |
| 4 | Google Drive export | planned |
| 5 | Tailscale Serve deployment | planned |
| 6 | iOS Shortcuts | planned |

## Development

Requires Python 3.11+ and Git 2.32+.

```sh
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Tests use temporary local bare repositories; they need no network, GitHub,
Google or Tailscale access.

## Configuration

Copy [config/example.yaml](config/example.yaml) to `config/local.yaml`
(git-ignored). Secrets (GitHub token, bearer token, Google credentials) live in
files outside the repository and are referenced by path.

## License

GPL-3.0 — see [LICENSE](LICENSE).

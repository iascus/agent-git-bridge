"""Command line entry point: ``git-bridge``.

Configuration path: ``--config``, else ``$GIT_BRIDGE_CONFIG``, else
``~/.config/agent-git-bridge/config.yaml``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from .artifact import parse_artifact
from .config import Settings, is_loopback_host, load_settings
from .errors import BridgeError, ConfigError

DEFAULT_CONFIG = Path.home() / ".config" / "agent-git-bridge" / "config.yaml"
MIN_GIT_VERSION = (2, 32)


def _config_path(arg: str | None) -> Path:
    return Path(arg or os.environ.get("GIT_BRIDGE_CONFIG") or DEFAULT_CONFIG)


def _print(obj) -> None:
    data = obj.model_dump() if hasattr(obj, "model_dump") else obj
    print(json.dumps(data, indent=2))


def _bridge(settings: Settings):
    from .repository import Bridge, default_store_factory

    return Bridge(settings, store_factory=default_store_factory(settings))


def serve(settings: Settings) -> int:
    import uvicorn

    from .api import create_app

    host, port = settings.server.host, settings.server.port
    if not is_loopback_host(host):  # also enforced by configuration validation
        raise ConfigError("refusing to listen on a non-loopback address")
    app = create_app(settings)
    uvicorn.run(
        app,
        host=host,
        port=port,
        workers=1,  # locks are in-process
        proxy_headers=False,
        server_header=False,
        log_level="info",
    )
    return 0


def git_version() -> tuple[int, int]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def check(settings: Settings) -> int:
    """Diagnose configuration and credentials without changing anything."""
    ok = True

    def report(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"[{'ok' if good else 'FAIL'}] {name}{': ' + detail if detail else ''}")

    version = git_version()
    report("git version", version >= MIN_GIT_VERSION, ".".join(map(str, version)))
    report("listen address is loopback", is_loopback_host(settings.server.host), f"{settings.server.host}:{settings.server.port}")
    try:
        from .auth import BearerTokenAuthenticator

        BearerTokenAuthenticator.from_file(settings.server.bearer_token_file)
        report("bearer token", True)
    except ConfigError as exc:
        report("bearer token", False, exc.message)
    if settings.github_token_file is not None:
        report("GitHub token file", settings.github_token_file.is_file())
    if settings.snapshot.transport == "google_drive":
        report("Google client secrets", settings.google.client_secrets_file.is_file())
        try:
            from .drive import load_credentials

            load_credentials(settings.google.token_file)
            report("Google login", True)
        except BridgeError as exc:
            report("Google login", False, exc.message)
    bridge = _bridge(settings)
    for key in bridge.keys:
        repo = bridge.repository(key)
        try:
            repo.ensure_clone()
            for branch in repo.config.allowed_branches:
                sha = repo.fetch(branch)
                report(f"{key}: fetch {branch}", True, sha[:12])
        except BridgeError as exc:
            report(f"{key}: fetch", False, f"{exc.message} {exc.details.get('stderr', '')}".strip())
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="git-bridge", description="Guarded Git publication and snapshot export.")
    parser.add_argument("--config", help=f"configuration file (default {DEFAULT_CONFIG})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the HTTP service on the configured loopback address")
    sub.add_parser("check", help="verify configuration, credentials and remotes")
    sub.add_parser("google-login", help="one-time Google Drive consent in a browser")
    p_tok = sub.add_parser("init-token", help="generate the bearer token file for the iOS Shortcut")
    p_tok.add_argument("--force", action="store_true", help="replace an existing token")
    for name in ("status", "refresh"):
        sub.add_parser(name).add_argument("repo")
    for name in ("validate", "publish"):
        p = sub.add_parser(name, help=f"{name} a publish.zip from the local filesystem")
        p.add_argument("repo")
        p.add_argument("zip", type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings(_config_path(args.config))
        if args.command == "serve":
            return serve(settings)
        if args.command == "check":
            return check(settings)
        if args.command == "google-login":
            if settings.google is None:
                raise ConfigError("configuration has no google section")
            from .drive import google_login

            google_login(settings.google.client_secrets_file, settings.google.token_file)
            print(f"Google login stored in {settings.google.token_file}")
            return 0
        if args.command == "init-token":
            from .auth import write_token_file

            path = settings.server.bearer_token_file
            if path is None:
                raise ConfigError("server.bearer_token_file is not configured")
            write_token_file(path, overwrite=args.force)
            print(f"Bearer token written to {path}. Copy it into the iOS Shortcuts from that file.")
            return 0

        repo = _bridge(settings).repository(args.repo)
        if args.command == "status":
            result = repo.status()
        elif args.command == "refresh":
            result = repo.refresh()
        else:
            artifact = parse_artifact(args.zip.read_bytes(), settings.limits)
            result = repo.validate_patch(artifact) if args.command == "validate" else repo.publish_and_refresh(artifact)
        _print(result)
        return 0 if getattr(result, "ok", True) else 1
    except BridgeError as exc:
        print(f"error ({exc.code}): {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

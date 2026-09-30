from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import GitEnv, artifact_zip, git
from git_bridge import cli
from git_bridge.api import create_app
from git_bridge.config import Limits, ServerConfig, Settings, load_settings
from git_bridge.errors import ConfigError

TOKEN = "t0k3n-" + "x" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ROOT = Path(__file__).resolve().parents[1]


def _client(env: GitEnv, **settings_changes) -> TestClient:
    token_file = env.tmp / "bearer-token"
    token_file.write_text(TOKEN + "\n")
    server = ServerConfig(bearer_token_file=token_file)
    env.settings = env.settings.model_copy(update={"server": server, **settings_changes})
    return TestClient(create_app(env.settings, bridge=env.bridge))


@pytest.fixture
def client(gitenv: GitEnv) -> TestClient:
    return _client(gitenv)


# ------------------------------------------------------------------- health


def test_health_is_unauthenticated_and_reveals_nothing(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_interactive_docs_and_schema_are_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --------------------------------------------------------------- auth


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer wrong-token-" + "y" * 40},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
        {"Authorization": f"Bearer {TOKEN}extra"},
    ],
)
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/repos/rot3k/status"),
        ("post", "/repos/rot3k/refresh"),
        ("post", "/repos/rot3k/validate-patch"),
        ("post", "/repos/rot3k/push"),
    ],
)
def test_missing_or_invalid_token_is_401(client, method, path, headers):
    r = getattr(client, method)(path, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.json()["error"]["code"] == "unauthorized"


def test_unknown_repository_requires_auth_before_404(client):
    assert client.get("/repos/nope/status").status_code == 401
    r = client.get("/repos/nope/status", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_repository"


def test_tokens_never_logged(gitenv: GitEnv, caplog):
    audit = gitenv.tmp / "audit.jsonl"
    token_file = gitenv.tmp / "github-token"
    token_file.write_text("github_pat_SECRETSECRET")
    client = _client(gitenv, audit_log=audit, github_token_file=token_file)
    caplog.set_level(logging.DEBUG)
    base, patch = gitenv.make_patch({"MANIFEST.md": "x\n"})
    client.post("/repos/rot3k/push", content=artifact_zip(patch, base), headers={"Authorization": "Bearer wrong" + "z" * 40})
    client.post("/repos/rot3k/validate-patch", content=artifact_zip(patch, base), headers=AUTH)
    logged = caplog.text + audit.read_text()
    assert "auth_failed" in logged
    for secret in (TOKEN, "wrong" + "z" * 40, "SECRETSECRET"):
        assert secret not in logged


def test_app_refuses_to_start_without_token(gitenv: GitEnv):
    with pytest.raises(ConfigError):
        create_app(gitenv.settings, bridge=gitenv.bridge)


def test_app_refuses_short_token(gitenv: GitEnv):
    short = gitenv.tmp / "short"
    short.write_text("too-short")
    settings = gitenv.settings.model_copy(update={"server": ServerConfig(bearer_token_file=short)})
    with pytest.raises(ConfigError):
        create_app(settings, bridge=gitenv.bridge)


# ----------------------------------------------------------- endpoints


def test_status(client, gitenv: GitEnv):
    r = client.get("/repos/rot3k/status", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["working"] == {"branch": "design-docs", "commit": gitenv.head(), "error": None}
    assert body["integration"]["commit"] == gitenv.head("main")


def test_validate_patch_raw_body(client, gitenv: GitEnv):
    base, patch = gitenv.make_patch({"MANIFEST.md": "validated\n"})
    r = client.post(
        "/repos/rot3k/validate-patch",
        content=artifact_zip(patch, base),
        headers={**AUTH, "Content-Type": "application/zip"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["operation"] == "validate" and body["new_sha"] is None
    assert "Nothing was committed" in body["message"]
    assert gitenv.head() == base


def test_push_raw_body(client, gitenv: GitEnv):
    base, patch = gitenv.make_patch({"records/new.md": "hello\n"})
    r = client.post("/repos/rot3k/push", content=artifact_zip(patch, base), headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["git_push"] == "success"
    assert body["old_sha"] == base and body["new_sha"] == gitenv.head()
    assert body["changed_files"] == [{"path": "records/new.md", "insertions": 1, "deletions": 0}]
    assert body["validation"]["passed"]
    assert body["snapshot_refresh"] == "not_configured"
    assert body["operation"] == "push"
    assert body["message"].startswith(f"Pushed {body['new_sha'][:12]}")


def test_deprecated_publish_path_still_pushes(client, gitenv: GitEnv):
    base, patch = gitenv.make_patch({"records/alias.md": "alias\n"})
    r = client.post("/repos/rot3k/publish", content=artifact_zip(patch, base), headers=AUTH)
    assert r.status_code == 200 and r.json()["git_push"] == "success"
    assert gitenv.head() == r.json()["new_sha"]


def test_push_multipart_upload(client, gitenv: GitEnv):
    base, patch = gitenv.make_patch({"records/multi.md": "multipart\n"})
    r = client.post(
        "/repos/rot3k/push",
        files={"file": ("rot3k-push.zip", artifact_zip(patch, base), "application/zip")},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert git(gitenv.origin, "show", f"{r.json()['new_sha']}:records/multi.md") == "multipart"


def test_multipart_with_two_files_rejected(client, gitenv: GitEnv):
    base, patch = gitenv.make_patch({"a.md": "a\n"})
    z = artifact_zip(patch, base)
    r = client.post(
        "/repos/rot3k/push",
        files=[("file", ("a.zip", z, "application/zip")), ("file", ("b.zip", z, "application/zip"))],
        headers=AUTH,
    )
    assert r.status_code == 400
    assert gitenv.head() == base


def test_push_remote_changed_is_409(client, gitenv: GitEnv):
    base, patch = gitenv.make_patch({"MANIFEST.md": "late\n"})
    gitenv.advance_remote()
    r = client.post("/repos/rot3k/push", content=artifact_zip(patch, base), headers=AUTH)
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "remote_changed" and body["git_push"] == "failed"
    assert body["message"].startswith("Rejected (remote_changed)")


def test_malformed_upload_is_400(client):
    r = client.post("/repos/rot3k/push", content=b"not a zip", headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_artifact"


def test_oversized_upload_is_413(gitenv: GitEnv):
    client = _client(gitenv, limits=Limits(max_archive_bytes=1000))
    r = client.post("/repos/rot3k/push", content=b"\0" * 5000, headers=AUTH)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "artifact_too_large"


def test_disallowed_branch_is_403(client, gitenv: GitEnv):
    base, patch = gitenv.make_patch({"MANIFEST.md": "x\n"})
    r = client.post("/repos/rot3k/push", content=artifact_zip(patch, base, branch="main"), headers=AUTH)
    assert r.status_code == 403


def test_audit_log_is_append_only_jsonl(gitenv: GitEnv):
    audit = gitenv.tmp / "logs" / "audit.jsonl"
    client = _client(gitenv, audit_log=audit)
    base, patch = gitenv.make_patch({"a.md": "a\n"})
    client.post("/repos/rot3k/push", content=artifact_zip(patch, base), headers=AUTH)
    client.post("/repos/rot3k/push", content=artifact_zip(patch, base), headers=AUTH)  # now stale
    events = [json.loads(line) for line in audit.read_text().splitlines()]
    pushes = [e for e in events if e["event"] == "push"]
    assert [e["ok"] for e in pushes] == [True, False]
    assert pushes[1]["error"] == "remote_changed"
    assert pushes[0]["expected_sha"] == base and pushes[0]["new_sha"]
    assert all("ts" in e for e in events)


# ------------------------------------------------------------ loopback only


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "100.93.58.16", "example.com", ""])
def test_non_loopback_listen_address_rejected(host):
    with pytest.raises(ValueError):
        ServerConfig(host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.2"])
def test_loopback_listen_address_accepted(host):
    assert ServerConfig(host=host).host == host


def test_default_and_example_configuration_bind_to_loopback():
    assert ServerConfig().host == "127.0.0.1"
    for name in ("example.yaml", "example.windows.yaml"):
        settings = load_settings(ROOT / "config" / name)
        assert settings.server.host == "127.0.0.1"


def test_serve_binds_uvicorn_to_loopback(gitenv: GitEnv, monkeypatch):
    token_file = gitenv.tmp / "bearer-token"
    token_file.write_text(TOKEN)
    settings = gitenv.settings.model_copy(update={"server": ServerConfig(bearer_token_file=token_file)})
    captured = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    from git_bridge import instance

    calls = []
    monkeypatch.setattr(instance, "stop_existing_instances", lambda port, pid_file: calls.append((port, pid_file)) or [])
    monkeypatch.setattr(instance, "write_pid_file", lambda path: calls.append(("pid", path)))
    assert cli.serve(settings) == 0
    assert calls == [(8000, gitenv.settings.work_dir.parent / "git-bridge-serve.pid"), ("pid", gitenv.settings.work_dir.parent / "git-bridge-serve.pid")]
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8000
    assert captured["workers"] == 1


def test_serve_refuses_non_loopback_even_if_validation_bypassed(gitenv: GitEnv, monkeypatch):
    token_file = gitenv.tmp / "bearer-token"
    token_file.write_text(TOKEN)
    server = ServerConfig.model_construct(host="0.0.0.0", port=8000, bearer_token_file=token_file)
    settings = Settings.model_construct(**{**dict(gitenv.settings), "server": server})
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: pytest.fail("must not start"))
    with pytest.raises(ConfigError):
        cli.serve(settings)

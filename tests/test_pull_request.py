"""Opening a pull request after publication (GitHub API faked)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from conftest import BRANCH, GitEnv
from git_bridge.config import PullRequestConfig, RepositoryConfig
from git_bridge.github import GitHubClient, PullRequestError
from git_bridge.repository import Bridge


class FakeGitHub:
    def __init__(self, open_prs=None, fail=None):
        self.open_prs = open_prs or []
        self.fail = fail
        self.calls = []

    def find_open_pull_request(self, repo, head, base):
        self.calls.append(("find", repo, head, base))
        if self.fail == "find":
            raise PullRequestError("GitHub API 403: Resource not accessible by personal access token")
        for pr in self.open_prs:
            if pr["head"] == head and pr["base"] == base:
                return pr
        return None

    def create_pull_request(self, repo, head, base, title, body, draft):
        self.calls.append(("create", repo, head, base, title, draft))
        if self.fail == "create":
            raise PullRequestError("GitHub API 422: Validation Failed")
        pr = {"number": 7, "html_url": f"https://github.com/{repo}/pull/7", "head": head, "base": base, "body": body}
        self.open_prs.append(pr)
        return pr


@pytest.fixture
def prenv(gitenv: GitEnv) -> GitEnv:
    gitenv.with_repo_config(pull_request=PullRequestConfig())
    gitenv.github = FakeGitHub()
    return gitenv


def _push(env: GitEnv, message="Refine encounter UI\n\nLonger explanation."):
    base, patch = env.make_patch({"docs/pr.md": f"{len(env.github.calls)}\n"})
    return env.repo.push_and_refresh(env.artifact(patch, base, commit_message=message))


def test_first_publication_opens_pull_request(prenv: GitEnv):
    out = _push(prenv)
    assert out.ok and out.git_push == "success"
    assert out.pull_request.state == "created"
    assert (out.pull_request.number, out.pull_request.url) == (7, "https://github.com/iascus/rot3k/pull/7")
    assert prenv.github.calls == [
        ("find", "iascus/rot3k", BRANCH, "main"),
        ("create", "iascus/rot3k", BRANCH, "main", "Refine encounter UI", False),
    ]
    assert "Opened PR #7 into main" in out.message
    assert "never merges" in prenv.github.open_prs[0]["body"]


def test_later_publication_reuses_open_pull_request(prenv: GitEnv):
    _push(prenv)
    out = _push(prenv, "Second change")
    assert out.pull_request.state == "existing" and out.pull_request.number == 7
    assert [c[0] for c in prenv.github.calls] == ["find", "create", "find"]
    assert "PR #7 into main updated" in out.message


def test_pull_request_failure_does_not_fail_publication(prenv: GitEnv):
    prenv.github = FakeGitHub(fail="find")
    out = _push(prenv)
    assert out.ok and out.git_push == "success" and out.new_sha == prenv.head()
    assert out.pull_request.state == "failed" and "not accessible" in out.pull_request.error
    assert out.error is None
    assert "PR creation FAILED" in out.message


def test_rejected_publication_does_not_touch_pull_requests(prenv: GitEnv):
    base, patch = prenv.make_patch({"a.md": "a\n"})
    prenv.advance_remote()
    out = prenv.repo.push_and_refresh(prenv.artifact(patch, base))
    assert out.error.code == "remote_changed"
    assert out.pull_request is None and prenv.github.calls == []


def test_validate_patch_never_opens_pull_request(prenv: GitEnv):
    base, patch = prenv.make_patch({"a.md": "a\n"})
    assert prenv.repo.validate_patch(prenv.artifact(patch, base)).ok
    assert prenv.github.calls == []


def test_not_configured_means_no_pull_request(gitenv: GitEnv):
    gitenv.github = FakeGitHub()
    base, patch = gitenv.make_patch({"a.md": "a\n"})
    out = gitenv.repo.push_and_refresh(gitenv.artifact(patch, base))
    assert out.ok and out.pull_request is None and gitenv.github.calls == []


def test_draft_option_and_commit_message_not_exposed(prenv: GitEnv):
    prenv.with_repo_config(pull_request=PullRequestConfig(draft=True))
    out = _push(prenv, "Secret-ish subject line")
    assert prenv.github.calls[-1][-1] is True
    assert "commit_message" not in out.model_dump()


def test_real_client_only_for_github_remotes(tmp_path):
    token = tmp_path / "t"
    token.write_text("github_pat_x")
    def settings(remote_url):
        from git_bridge.config import Settings

        cfg = RepositoryConfig(
            github_repo="iascus/rt3k", local_path=tmp_path / "r", remote_url=remote_url,
            integration_branch="main", working_branch="design-docs", pull_request=PullRequestConfig(),
        )
        return Settings(work_dir=tmp_path / "w", github_token_file=token, repositories={"rot3k": cfg})

    assert isinstance(Bridge(settings(None)).repository("rot3k").github, GitHubClient)
    assert Bridge(settings(str(tmp_path / "local.git"))).repository("rot3k").github is None


def test_pull_request_targets_configured_integration_branch(prenv: GitEnv):
    prenv.with_repo_config(integration_branch="trunk")
    _push(prenv)
    assert prenv.github.calls[0] == ("find", "iascus/rot3k", BRANCH, "trunk")


# ------------------------------------------------------------ HTTP client


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_client_requests(monkeypatch):
    sent = []

    def fake_urlopen(req, timeout):
        sent.append(req)
        if req.get_method() == "GET":
            return _Resp(b"[]")
        return _Resp(json.dumps({"number": 3, "html_url": "u"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = GitHubClient(lambda: "github_pat_SECRET")
    assert client.find_open_pull_request("iascus/rt3k", "design-docs", "poc1") is None
    get = sent[0]
    assert get.full_url == "https://api.github.com/repos/iascus/rt3k/pulls?state=open&head=iascus%3Adesign-docs&base=poc1&per_page=5"
    assert get.get_header("Authorization") == "Bearer github_pat_SECRET"

    assert client.create_pull_request("iascus/rt3k", "design-docs", "poc1", "T", "B", False)["number"] == 3
    body = json.loads(sent[1].data)
    assert body == {"title": "T", "head": "design-docs", "base": "poc1", "body": "B", "draft": False, "maintainer_can_modify": False}


def test_client_error_explains_missing_permission_without_leaking_token(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"message": "Resource not accessible by personal access token"}')
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(PullRequestError) as exc:
        GitHubClient(lambda: "github_pat_SECRET").find_open_pull_request("iascus/rt3k", "design-docs", "poc1")
    assert "Pull requests: Read and write" in exc.value.message
    assert "SECRET" not in exc.value.message

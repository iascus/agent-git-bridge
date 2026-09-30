"""Minimal GitHub REST client: find or open pull requests, and look up the
latest merged one. Nothing else.

The bridge never merges, closes, edits or comments on pull requests.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol

from .errors import BridgeError

API_URL = "https://api.github.com"


class PullRequestError(BridgeError):
    code = "pull_request_failed"
    http_status = 502


class GitHubApi(Protocol):
    def find_open_pull_request(self, repo: str, head: str, base: str) -> dict[str, Any] | None: ...
    def create_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str, draft: bool
    ) -> dict[str, Any]: ...
    def latest_merged_pull_request(self, repo: str, head: str, base: str) -> dict[str, Any] | None: ...


class GitHubClient:
    def __init__(self, token_reader: Callable[[], str], *, api_url: str = API_URL, timeout: int = 30) -> None:
        self._token_reader = token_reader
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.api_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token_reader()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "agent-git-bridge",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc)
            except ValueError:
                detail = {}
            message = str(detail.get("message", exc.reason))[:300]
            errors = [str(e.get("message", e))[:200] for e in detail.get("errors", []) if isinstance(e, dict)]
            if exc.code in (401, 403, 404) and "not accessible" in message.lower():
                message += " (the token needs the 'Pull requests: Read and write' permission)"
            raise PullRequestError(f"GitHub API {exc.code}: {message}", errors=errors) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PullRequestError(f"GitHub API unreachable: {exc}") from None

    def find_open_pull_request(self, repo: str, head: str, base: str) -> dict[str, Any] | None:
        owner = repo.split("/", 1)[0]
        query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{head}", "base": base, "per_page": 5})
        found = self._request("GET", f"/repos/{repo}/pulls?{query}")
        return found[0] if found else None

    def latest_merged_pull_request(self, repo: str, head: str, base: str) -> dict[str, Any] | None:
        owner = repo.split("/", 1)[0]
        query = urllib.parse.urlencode(
            {"state": "closed", "head": f"{owner}:{head}", "base": base, "sort": "updated", "direction": "desc", "per_page": 30}
        )
        merged = [pr for pr in self._request("GET", f"/repos/{repo}/pulls?{query}") if pr.get("merged_at")]
        return max(merged, key=lambda pr: pr["merged_at"]) if merged else None

    def create_pull_request(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body, "draft": draft, "maintainer_can_modify": False},
        )

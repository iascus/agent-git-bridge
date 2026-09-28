"""The single place that executes Git.

Rules enforced here:
- fixed argument arrays, never ``shell=True``;
- no inherited user/system Git configuration;
- no interactive prompts;
- Git may not discover a repository above the directory it was pointed at;
- credentials only reach network commands, via environment, never argv.
"""

from __future__ import annotations

import base64
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .errors import GitError

# Applied to every invocation for deterministic, non-interactive behaviour.
_FIXED_CONFIG = (
    "core.autocrlf=false",
    "core.fsmonitor=false",
    "core.quotePath=false",
    "advice.detachedHead=false",
    "gc.auto=0",
)

# Variables copied from the service environment; everything else is dropped.
_PASSTHROUGH_ENV = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)

STDERR_TAIL_CHARS = 2000


def minimal_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A scrubbed environment: no tokens, no inherited GIT_* variables."""
    env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ}
    env.update(
        {
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if extra:
        env.update(extra)
    return env


def github_auth_env(token: str) -> dict[str, str]:
    """Inject a GitHub token as an HTTP header through GIT_CONFIG_* variables.

    Works for fine-grained PATs and GitHub App installation tokens. The value
    never appears in argv (visible to other local users) or on disk.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def tail(text: str, limit: int = STDERR_TAIL_CHARS) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: str

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace").strip()


class Git:
    """Runs git with ``cwd`` as the repository (bare clone or worktree)."""

    def __init__(self, cwd: Path, *, timeout: int = 300, extra_config: Sequence[str] = ()) -> None:
        self.cwd = Path(cwd)
        self.timeout = timeout
        # Validated against an allowlist in configuration.
        self.extra_config = tuple(extra_config)

    def at(self, cwd: Path) -> "Git":
        """Same settings, different repository directory (e.g. a worktree)."""
        return Git(cwd, timeout=self.timeout, extra_config=self.extra_config)

    def run(
        self,
        args: Sequence[str],
        *,
        input: bytes | None = None,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> GitResult:
        if isinstance(args, (str, bytes)) or not all(isinstance(a, str) for a in args):
            raise TypeError("git arguments must be a sequence of str")
        argv = ["git"]
        for item in (*_FIXED_CONFIG, *self.extra_config):
            argv += ["-c", item]
        argv += list(args)

        run_env = minimal_env(env)
        # Never walk up from cwd into some unrelated enclosing repository.
        run_env["GIT_CEILING_DIRECTORIES"] = str(self.cwd.resolve().parent)

        try:
            proc = subprocess.run(
                argv,
                cwd=self.cwd,
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                env=run_env,
                timeout=timeout or self.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out") from exc
        except OSError as exc:
            raise GitError(f"cannot execute git: {exc.strerror}") from exc

        result = GitResult(proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace"))
        if check and proc.returncode != 0:
            raise GitError(
                f"git {args[0]} failed with exit status {proc.returncode}",
                returncode=proc.returncode,
                stderr=tail(result.stderr),
            )
        return result

    def text(self, args: Sequence[str], **kwargs) -> str:
        return self.run(args, **kwargs).text

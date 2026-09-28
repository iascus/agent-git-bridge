"""Repository-path globbing with ``**`` support.

``*`` and ``?`` never match ``/``; ``**/`` matches zero or more directories;
a trailing ``**`` matches everything below. Matching is case-sensitive and
operates on POSIX repository-relative paths.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif c == "*":
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def match(path: str, pattern: str) -> bool:
    return _compile(pattern).match(path) is not None


def match_any(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(match(path, p) for p in patterns)

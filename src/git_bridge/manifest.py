"""Export selection from a repository's own source manifest.

A manifest (e.g. ``docs/design/MANIFEST.md``) carries a machine-readable
YAML block between HTML comment markers:

    <!-- PROJECT_SOURCE_FILES_BEGIN -->
    ```yaml
    project_source_files:
      - docs/design/MANIFEST.md
      - AGENTS.md
    ```
    <!-- PROJECT_SOURCE_FILES_END -->

The manifest is always read from the same commit whose files are exported.

A second, optional block declares **project artifact roots**: repository
directories whose every tracked Git file is exported as-is, text or binary,
without the manifest listing each file individually:

    <!-- PROJECT_ARTIFACT_ROOTS_BEGIN -->
    ```yaml
    project_artifact_roots:
      - assets/gfx/
    ```
    <!-- PROJECT_ARTIFACT_ROOTS_END -->

Other blocks in the manifest are ignored by the bridge.
"""

from __future__ import annotations

import re

import yaml

from .errors import SnapshotError
from .store import SNAPSHOT_NAME, check_relative_path

FILES_BLOCK = "PROJECT_SOURCE_FILES"
ARTIFACT_ROOTS_BLOCK = "PROJECT_ARTIFACT_ROOTS"


def _block(text: str, name: str) -> object | None:
    pattern = re.compile(rf"<!--\s*{name}_BEGIN\s*-->(.*?)<!--\s*{name}_END\s*-->", re.DOTALL)
    found = pattern.findall(text)
    if not found:
        return None
    if len(found) > 1:
        raise SnapshotError(f"manifest: {name} block appears more than once")
    body = found[0].strip()
    fence = re.match(r"^```[A-Za-z]*\s*\n(.*?)\n?```$", body, re.DOTALL)
    if fence:
        body = fence.group(1)
    try:
        return yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise SnapshotError(f"manifest: {name} block is not valid YAML: {exc}") from exc


def parse_manifest(content: bytes, manifest_path: str) -> list[str]:
    """The ordered, validated ``project_source_files`` list."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"manifest {manifest_path} is not UTF-8") from exc
    block = _block(text, FILES_BLOCK)
    if not isinstance(block, dict) or "project_source_files" not in block:
        raise SnapshotError(f"manifest {manifest_path} has no {FILES_BLOCK} block with project_source_files")
    files = block["project_source_files"]
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise SnapshotError("manifest: project_source_files must be a list of strings")
    if not files:
        raise SnapshotError("manifest: project_source_files is empty")
    seen: set[str] = set()
    for path in files:
        if path != SNAPSHOT_NAME:  # reserved name: reported as not exported, not fatal
            check_relative_path(path)
        if path in seen:
            raise SnapshotError(f"manifest: {path!r} is listed twice")
        seen.add(path)
    return files


def check_artifact_root(root: str) -> str:
    """Validate and normalise one artifact root to ``a/b/`` (trailing slash,
    no leading slash, no ``.``/``..`` components)."""
    if not isinstance(root, str):
        raise SnapshotError("project_artifact_roots entries must be strings")
    stripped = root[:-1] if root.endswith("/") else root
    if not stripped:
        raise SnapshotError(f"invalid project_artifact_roots entry {root!r}: must not be empty")
    try:
        check_relative_path(f"{stripped}/.gb-placeholder")
    except SnapshotError:
        raise SnapshotError(f"invalid project_artifact_roots entry {root!r}") from None
    return stripped + "/"


def parse_artifact_roots(content: bytes, manifest_path: str) -> list[str]:
    """The ordered, validated ``project_artifact_roots`` list, or ``[]`` if the
    manifest declares no :data:`ARTIFACT_ROOTS_BLOCK` block at all."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"manifest {manifest_path} is not UTF-8") from exc
    block = _block(text, ARTIFACT_ROOTS_BLOCK)
    if block is None:
        return []
    if not isinstance(block, dict) or "project_artifact_roots" not in block:
        raise SnapshotError(
            f"manifest {manifest_path} has a {ARTIFACT_ROOTS_BLOCK} block with no project_artifact_roots"
        )
    roots = block["project_artifact_roots"]
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        raise SnapshotError("manifest: project_artifact_roots must be a list of strings")
    normalised: list[str] = []
    seen: set[str] = set()
    for root in roots:
        checked = check_artifact_root(root)
        if checked in seen:
            raise SnapshotError(f"manifest: artifact root {checked!r} is listed twice")
        seen.add(checked)
        normalised.append(checked)
    return normalised

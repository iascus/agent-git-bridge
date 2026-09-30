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
Other blocks in the manifest are ignored by the bridge.
"""

from __future__ import annotations

import re

import yaml

from .errors import SnapshotError
from .store import SNAPSHOT_NAME, check_relative_path

FILES_BLOCK = "PROJECT_SOURCE_FILES"


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

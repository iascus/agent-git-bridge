"""Export selection from a repository's own source manifest.

A manifest (e.g. ``docs/design/MANIFEST.md``) carries machine-readable YAML
blocks between HTML comment markers:

    <!-- PROJECT_SOURCE_FILES_BEGIN -->
    ```yaml
    project_source_files:
      - docs/design/MANIFEST.md
      - AGENTS.md
    ```
    <!-- PROJECT_SOURCE_FILES_END -->

and optionally:

    <!-- PROJECT_SOURCE_MATERIALIZATION_BEGIN -->
    ```yaml
    project_source_materialization:
      default: bootstrap
      lazy:
        character_dossiers:
          globs: [docs/characters/records-juan-*/*.md]
          paths: []
    ```
    <!-- PROJECT_SOURCE_MATERIALIZATION_END -->

The manifest is read from the same commit that is being exported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from . import pathglob
from .errors import SnapshotError
from .store import check_relative_path

FILES_BLOCK = "PROJECT_SOURCE_FILES"
MATERIALIZATION_BLOCK = "PROJECT_SOURCE_MATERIALIZATION"
_CLASSES = ("bootstrap", "lazy")


@dataclass(frozen=True)
class ManifestSelection:
    files: list[str]
    default_class: str = "bootstrap"
    # lazy class name -> (globs, paths)
    lazy_classes: dict[str, tuple[list[str], list[str]]] = field(default_factory=dict)

    def classify(self, path: str) -> tuple[str, str | None]:
        """(class, lazy class name). Raises if several lazy classes match."""
        matches = [
            name
            for name, (globs, paths) in self.lazy_classes.items()
            if path in paths or pathglob.match_any(path, globs)
        ]
        if len(matches) > 1:
            raise SnapshotError(f"manifest: {path!r} matches several lazy classes: {', '.join(matches)}")
        if matches:
            return "lazy", matches[0]
        return self.default_class, None


def _block(text: str, name: str) -> object | None:
    pattern = re.compile(
        rf"<!--\s*{name}_BEGIN\s*-->(.*?)<!--\s*{name}_END\s*-->", re.DOTALL
    )
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


def _str_list(value: object, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SnapshotError(f"manifest: {what} must be a list of strings")
    return value


def parse_manifest(content: bytes, manifest_path: str) -> ManifestSelection:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"manifest {manifest_path} is not UTF-8") from exc

    files_block = _block(text, FILES_BLOCK)
    if not isinstance(files_block, dict) or "project_source_files" not in files_block:
        raise SnapshotError(f"manifest {manifest_path} has no {FILES_BLOCK} block with project_source_files")
    files = _str_list(files_block["project_source_files"], "project_source_files")
    if not files:
        raise SnapshotError("manifest: project_source_files is empty")
    seen: set[str] = set()
    for path in files:
        check_relative_path(path)
        if path in seen:
            raise SnapshotError(f"manifest: {path!r} is listed twice")
        seen.add(path)

    materialization = _block(text, MATERIALIZATION_BLOCK)
    if materialization is None:
        return ManifestSelection(files=files)
    policy = materialization.get("project_source_materialization") if isinstance(materialization, dict) else None
    if not isinstance(policy, dict):
        raise SnapshotError("manifest: materialization block needs project_source_materialization")
    default = policy.get("default", "bootstrap")
    if default not in _CLASSES:
        raise SnapshotError(f"manifest: materialization default must be one of {_CLASSES}")
    lazy: dict[str, tuple[list[str], list[str]]] = {}
    for name, spec in (policy.get("lazy") or {}).items():
        if not isinstance(spec, dict):
            raise SnapshotError(f"manifest: lazy class {name!r} must be a mapping")
        lazy[str(name)] = (_str_list(spec.get("globs"), f"{name}.globs"), _str_list(spec.get("paths"), f"{name}.paths"))
    return ManifestSelection(files=files, default_class=default, lazy_classes=lazy)

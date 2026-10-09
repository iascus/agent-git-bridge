"""Generation building: integration snapshot + integration -> working overlay.

The central invariant: unzip(snapshot) + git apply(overlay) == sel(D),
byte for byte, whatever the branches' histories look like.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from conftest import BRANCH, INTEGRATION, GitEnv, git
from git_bridge.config import ExportConfig
from git_bridge.errors import SnapshotError
from git_bridge.manifest import parse_artifact_roots, parse_manifest
from git_bridge.snapshot import build_generation
from git_bridge.store import git_blob_sha1

MANIFEST_PATH = "docs/design/MANIFEST.md"
EXPORT = ExportConfig(drive_root="ChatGPT/rot3k", manifest=MANIFEST_PATH)


def manifest_md(*paths: str, roots: tuple[str, ...] = ()) -> str:
    listed = "\n".join(f"  - {p}" for p in (MANIFEST_PATH, *paths))
    text = (
        "# Manifest\n\nProse mentioning docs/ignored.md is not part of the inventory.\n\n"
        "<!-- PROJECT_SOURCE_FILES_BEGIN -->\n```yaml\nproject_source_files:\n"
        f"{listed}\n```\n<!-- PROJECT_SOURCE_FILES_END -->\n"
    )
    if roots:
        root_lines = "\n".join(f"  - {r}" for r in roots)
        text += (
            "\n<!-- PROJECT_ARTIFACT_ROOTS_BEGIN -->\n```yaml\nproject_artifact_roots:\n"
            f"{root_lines}\n```\n<!-- PROJECT_ARTIFACT_ROOTS_END -->\n"
        )
    return text


def png(tag: bytes = b"") -> bytes:
    """Deterministic binary content (always contains a NUL byte, like a real PNG)."""
    return b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + tag


BASE_FILES = {
    "AGENTS.md": "Agents\n",
    "docs/design/RULES.md": "Rule one.\nRule two.\nRule three.\n",
    "docs/design/LORE.md": "Once upon a time.\n" * 20,
    "docs/characters/liu-bei.md": "Liu Bei\n",
    "src/app.py": "print('not in the manifest')\n",
}
BASE_LISTED = ["AGENTS.md", "docs/design/RULES.md", "docs/design/LORE.md", "docs/characters/liu-bei.md"]


@pytest.fixture
def proj(gitenv: GitEnv) -> GitEnv:
    """main and design-docs both at a commit with a manifest and files."""
    base = gitenv.commit_to(INTEGRATION, {MANIFEST_PATH: manifest_md(*BASE_LISTED), **BASE_FILES}, "project base")
    gitenv.set_branch(BRANCH, base)
    gitenv.with_repo_config(export=EXPORT)
    return gitenv


def generate(env: GitEnv):
    repo = env.repo
    repo.ensure_clone()
    m, d = repo.fetch_heads()
    return build_generation(
        repo.git,
        integration_commit=m,
        working_commit=d,
        repository=env.github_repo,
        project_key=env.key,
        integration_branch=INTEGRATION,
        working_branch=BRANCH,
        export=EXPORT,
        snapshot_name="rot3k-snapshot.zip",
        diff_name="rot3k-working.diff",
    )


def unzip(data: bytes) -> tuple[dict, dict[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        contents = {i.filename: zf.read(i) for i in zf.infolist()}
    return json.loads(contents.pop("snapshot.json")), contents


def reconstruct(tmp: Path, snapshot_zip: bytes, overlay: bytes) -> dict[str, bytes]:
    """What a reader does: unzip the snapshot, then `git apply` the overlay."""
    manifest, files = unzip(snapshot_zip)
    work = tmp / "reconstructed"
    for rel, data in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_bytes(data)
    work.mkdir(exist_ok=True)
    if not manifest["working_diff"]["empty"]:
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        proc = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=nowarn", "-"],
            cwd=work, input=overlay, capture_output=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr.decode()
    return {p.relative_to(work).as_posix(): p.read_bytes() for p in work.rglob("*") if p.is_file()}


def selected_tree(env: GitEnv, commit: str) -> dict[str, bytes]:
    """sel(commit): the files commit's own manifest lists (text only) plus every
    tracked file under its own declared artifact roots (text or binary),
    computed independently of git_bridge.snapshot via raw Git plumbing."""
    manifest_bytes = git_show(env, commit, MANIFEST_PATH)
    listed = parse_manifest(manifest_bytes, MANIFEST_PATH)
    roots = parse_artifact_roots(manifest_bytes, MANIFEST_PATH)

    def under_root(path: str) -> bool:
        return any(path.startswith(r) for r in roots)

    out: dict[str, bytes] = {}
    for path in listed:
        # Reserved, or under a root: the artifact-root rule below always wins
        # (it must include a binary file even if also individually listed).
        if path == "snapshot.json" or under_root(path):
            continue
        data = git_show(env, commit, path, missing_ok=True)
        if data is not None and b"\0" not in data:
            out[path] = data
    if roots:
        tracked = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "-z", commit], cwd=env.origin, capture_output=True, check=True
        ).stdout.decode("utf-8", errors="surrogateescape").split("\0")
        for path in tracked:
            if not path or not under_root(path):
                continue
            out[path] = git_show(env, commit, path)
    return out


def git_show(env: GitEnv, commit: str, path: str, missing_ok: bool = False) -> bytes | None:
    proc = subprocess.run(["git", "cat-file", "blob", f"{commit}:{path}"], cwd=env.origin, capture_output=True)
    if proc.returncode != 0:
        assert missing_ok, proc.stderr
        return None
    return proc.stdout


def assert_reconstructs(env: GitEnv, tmp: Path):
    gen = generate(env)
    rebuilt = reconstruct(tmp, gen.snapshot_zip, gen.diff_bytes)
    expected = selected_tree(env, env.head(BRANCH))
    assert rebuilt == expected  # byte-for-byte, same path set
    # snapshot.json's working_files describe exactly the reconstructed tree
    assert gen.manifest["working_diff"]["working_files"] == {p: git_blob_sha1(b) for p, b in expected.items()}
    return gen


# ------------------------------------------------------------ the snapshot


def test_snapshot_is_the_integration_branch(proj: GitEnv):
    proj.commit_to(BRANCH, {"AGENTS.md": "working only\n"})
    gen = generate(proj)
    m, files = unzip(gen.snapshot_zip)
    assert m["format_version"] == 2
    assert (m["integration_branch"], m["integration_commit"]) == (INTEGRATION, proj.head(INTEGRATION))
    assert (m["working_branch"], m["working_commit"]) == (BRANCH, proj.head(BRANCH))
    assert m["repository"] == "iascus/rot3k" and m["project_key"] == "rot3k"
    assert files["AGENTS.md"] == b"Agents\n"  # main's content, not the working branch's
    assert set(files) == {MANIFEST_PATH, *BASE_LISTED}
    assert "src/app.py" not in files
    for path, info in m["files"].items():
        assert info["blob_sha"] == git_blob_sha1(files[path])
        assert info["blob_sha"] == git(proj.origin, "rev-parse", f"{m['integration_commit']}:{path}")


def test_overlay_metadata_pins_the_diff(proj: GitEnv):
    proj.commit_to(BRANCH, {"AGENTS.md": "changed\n"})
    gen = generate(proj)
    wd = gen.manifest["working_diff"]
    assert wd["sha256"] == hashlib.sha256(gen.diff_bytes).hexdigest()
    assert wd["bytes"] == len(gen.diff_bytes)
    assert (wd["base_commit"], wd["target_commit"]) == (proj.head(INTEGRATION), proj.head(BRANCH))
    assert (wd["files_changed"], wd["insertions"], wd["deletions"]) == (1, 1, 1)
    header = gen.diff_bytes.split(b"\n\n", 1)[0].decode()
    assert f"# generation: {gen.generation_id}" in header
    assert f"# base: main {proj.head(INTEGRATION)}" in header
    assert f"# target: design-docs {proj.head(BRANCH)}" in header


# ------------------------------------------------------- reconstruction


def test_modified_new_and_deleted_files(proj: GitEnv, tmp_path):
    listed = [p for p in BASE_LISTED if p != "docs/characters/liu-bei.md"] + ["docs/design/NEW.md"]
    proj.commit_to(
        BRANCH,
        {
            MANIFEST_PATH: manifest_md(*listed),
            "docs/design/RULES.md": "Rule one.\nRule two, amended.\nRule three.\n",
            "docs/design/NEW.md": "Brand new\n",
            "docs/characters/liu-bei.md": None,
        },
    )
    gen = assert_reconstructs(proj, tmp_path)
    body = gen.diff_bytes.decode()
    assert "new file mode" in body and "deleted file mode" in body
    assert not gen.manifest["working_diff"]["empty"]


def test_rename_is_represented_and_reconstructs(proj: GitEnv, tmp_path):
    listed = [p if p != "docs/design/LORE.md" else "docs/design/HISTORY.md" for p in BASE_LISTED]
    proj.commit_to(
        BRANCH,
        {MANIFEST_PATH: manifest_md(*listed), "docs/design/LORE.md": None, "docs/design/HISTORY.md": BASE_FILES["docs/design/LORE.md"]},
    )
    gen = assert_reconstructs(proj, tmp_path)
    body = gen.diff_bytes.decode()
    assert "rename from docs/design/LORE.md" in body and "rename to docs/design/HISTORY.md" in body


def test_identical_trees_give_an_empty_overlay(proj: GitEnv, tmp_path):
    gen = assert_reconstructs(proj, tmp_path)
    wd = gen.manifest["working_diff"]
    assert wd["empty"] and wd["files_changed"] == 0
    assert gen.diff_bytes.split(b"\n\n", 1)[1] == b""  # only the header
    assert b"# empty:" in gen.diff_bytes


def test_different_commits_with_identical_selected_trees_give_empty_overlay(proj: GitEnv, tmp_path):
    proj.commit_to(BRANCH, {"src/app.py": "print('changed outside the manifest')\n"})
    assert proj.head(BRANCH) != proj.head(INTEGRATION)
    gen = assert_reconstructs(proj, tmp_path)
    assert gen.manifest["working_diff"]["empty"]
    assert gen.manifest["working_ahead_by"] == 1


def test_integration_ahead_of_working_is_represented_faithfully(proj: GitEnv, tmp_path):
    """main advanced, design-docs not rebased: the overlay 'reverts' main-only changes."""
    proj.commit_to(INTEGRATION, {"docs/design/RULES.md": "Rule one.\nRule two.\nRule three.\nRule four (main only).\n"})
    gen = assert_reconstructs(proj, tmp_path)
    assert (gen.manifest["working_ahead_by"], gen.manifest["working_behind_by"]) == (0, 1)
    assert gen.manifest["merge_base_commit"] == proj.head(BRANCH)
    assert "-Rule four (main only)." in gen.diff_bytes.decode()


def test_divergent_branches(proj: GitEnv, tmp_path):
    base = proj.head(INTEGRATION)
    proj.commit_to(INTEGRATION, {"AGENTS.md": "Agents (main)\n", "docs/design/RULES.md": "main rules\n"})
    proj.commit_to(BRANCH, {"docs/characters/liu-bei.md": "Liu Bei (working)\n"})
    gen = assert_reconstructs(proj, tmp_path)
    m = gen.manifest
    assert m["merge_base_commit"] == base
    assert (m["working_ahead_by"], m["working_behind_by"]) == (1, 1)


def test_file_added_to_working_manifest_only(proj: GitEnv, tmp_path):
    """Unchanged between the trees, but only D's manifest lists it: the overlay adds it."""
    proj.commit_to(INTEGRATION, {"docs/design/EXTRA.md": "present on both branches\n"})
    proj.set_branch(BRANCH, proj.head(INTEGRATION))
    proj.commit_to(BRANCH, {MANIFEST_PATH: manifest_md(*BASE_LISTED, "docs/design/EXTRA.md")})
    gen = assert_reconstructs(proj, tmp_path)
    assert "docs/design/EXTRA.md" not in unzip(gen.snapshot_zip)[1]
    assert "new file mode" in gen.diff_bytes.decode()


def test_missing_and_binary_listed_files_are_excluded_on_both_sides(proj: GitEnv, tmp_path):
    listed = [*BASE_LISTED, "docs/missing.md", "docs/logo.bin"]
    proj.commit_to(INTEGRATION, {MANIFEST_PATH: manifest_md(*listed), "docs/logo.bin": bytes(range(256))})
    proj.set_branch(BRANCH, proj.head(INTEGRATION))
    proj.commit_to(BRANCH, {"docs/logo.bin": bytes(range(255, -1, -1))})
    gen = assert_reconstructs(proj, tmp_path)
    assert gen.manifest["not_exported"] == {"docs/logo.bin": "binary", "docs/missing.md": "missing"}
    assert gen.manifest["working_diff"]["empty"]  # binary change is invisible to the text export


# --------------------------------------------------------- artifact roots


def test_project_without_artifact_roots_stays_format_2(proj: GitEnv, tmp_path):
    proj.commit_to(BRANCH, {"AGENTS.md": "changed\n"})
    gen = assert_reconstructs(proj, tmp_path)
    assert gen.manifest["format_version"] == 2
    assert "artifact_roots" not in gen.manifest
    assert "origin" not in next(iter(gen.manifest["files"].values()))
    assert "working_artifact_files" not in gen.manifest["working_diff"]


def test_empty_artifact_root_does_not_break_export(proj: GitEnv, tmp_path):
    """A declared root with no tracked files under it must not break pull."""
    proj.commit_to(INTEGRATION, {MANIFEST_PATH: manifest_md(*BASE_LISTED, roots=("assets/gfx/",))})
    proj.set_branch(BRANCH, proj.head(INTEGRATION))
    gen = assert_reconstructs(proj, tmp_path)
    assert gen.manifest["format_version"] == 3
    assert gen.manifest["artifact_roots"] == ["assets/gfx/"]
    assert not any(p.startswith("assets/gfx/") for p in gen.manifest["files"])
    assert gen.manifest["working_diff"]["empty"]


def test_artifact_file_present_on_both_branches_is_byte_preserved(proj: GitEnv, tmp_path):
    base = proj.commit_to(
        INTEGRATION,
        {MANIFEST_PATH: manifest_md(*BASE_LISTED, roots=("assets/gfx/",)), "assets/gfx/logo.png": png(b"logo")},
    )
    proj.set_branch(BRANCH, base)
    gen = assert_reconstructs(proj, tmp_path)
    m, files = unzip(gen.snapshot_zip)
    assert files["assets/gfx/logo.png"] == png(b"logo")  # stored exactly, no decode/re-encode
    assert m["files"]["assets/gfx/logo.png"]["origin"] == "artifact_root"
    assert m["files"][MANIFEST_PATH]["origin"] == "source"
    assert m["files"]["assets/gfx/logo.png"]["blob_sha"] == git_blob_sha1(png(b"logo"))


def test_artifact_root_wins_even_if_the_file_is_also_listed_as_source(proj: GitEnv, tmp_path):
    """A binary file both listed in PROJECT_SOURCE_FILES and under an artifact
    root must still be exported (the root rule wins), not dropped as 'binary'."""
    base = proj.commit_to(
        INTEGRATION,
        {
            MANIFEST_PATH: manifest_md(*BASE_LISTED, "assets/gfx/logo.png", roots=("assets/gfx/",)),
            "assets/gfx/logo.png": png(b"both"),
        },
    )
    proj.set_branch(BRANCH, base)
    gen = assert_reconstructs(proj, tmp_path)
    assert "assets/gfx/logo.png" not in gen.manifest["not_exported"]
    assert gen.manifest["files"]["assets/gfx/logo.png"]["origin"] == "artifact_root"
    assert unzip(gen.snapshot_zip)[1]["assets/gfx/logo.png"] == png(b"both")


def test_artifact_binary_added_on_working_branch(proj: GitEnv, tmp_path):
    proj.commit_to(BRANCH, {MANIFEST_PATH: manifest_md(*BASE_LISTED, roots=("assets/gfx/",)), "assets/gfx/new.png": png(b"new")})
    gen = assert_reconstructs(proj, tmp_path)
    assert gen.manifest["format_version"] == 3  # D's manifest alone already declares roots
    assert gen.manifest["artifact_roots"] == []  # M's own manifest declares none yet
    body = gen.diff_bytes.decode()
    assert "new file mode" in body and "GIT binary patch" in body
    assert gen.manifest["working_diff"]["working_artifact_roots"] == ["assets/gfx/"]
    assert gen.manifest["working_diff"]["working_artifact_files"] == ["assets/gfx/new.png"]
    assert gen.manifest["working_diff"]["working_files"]["assets/gfx/new.png"] == git_blob_sha1(png(b"new"))


def test_artifact_binary_modified_between_branches(proj: GitEnv, tmp_path):
    base = proj.commit_to(
        INTEGRATION,
        {MANIFEST_PATH: manifest_md(*BASE_LISTED, roots=("assets/gfx/",)), "assets/gfx/logo.png": png(b"v1")},
    )
    proj.set_branch(BRANCH, base)
    proj.commit_to(BRANCH, {"assets/gfx/logo.png": png(b"v2-modified")})
    gen = assert_reconstructs(proj, tmp_path)
    body = gen.diff_bytes.decode()
    assert "GIT binary patch" in body and "new file mode" not in body and "deleted file mode" not in body
    assert gen.manifest["working_diff"]["working_files"]["assets/gfx/logo.png"] == git_blob_sha1(png(b"v2-modified"))


def test_artifact_binary_deleted_on_working_branch(proj: GitEnv, tmp_path):
    base = proj.commit_to(
        INTEGRATION,
        {MANIFEST_PATH: manifest_md(*BASE_LISTED, roots=("assets/gfx/",)), "assets/gfx/logo.png": png()},
    )
    proj.set_branch(BRANCH, base)
    proj.commit_to(BRANCH, {"assets/gfx/logo.png": None})
    gen = assert_reconstructs(proj, tmp_path)
    assert "deleted file mode" in gen.diff_bytes.decode()
    assert "assets/gfx/logo.png" not in gen.manifest["working_diff"]["working_files"]


def test_mixed_text_and_several_binary_changes_in_one_overlay(proj: GitEnv, tmp_path):
    # remove.png and added.png must be dissimilar enough that -M rename
    # detection does not fold this delete+add pair into a single rename.
    remove_content = bytes((i * 7 + 3) % 256 for i in range(300))
    added_content = bytes((i * 13 + 5) % 256 for i in range(900))
    base = proj.commit_to(
        INTEGRATION,
        {
            MANIFEST_PATH: manifest_md(*BASE_LISTED, roots=("assets/gfx/",)),
            "assets/gfx/keep.png": png(b"keep"),
            "assets/gfx/modify.png": png(b"before"),
            "assets/gfx/remove.png": remove_content,
        },
    )
    proj.set_branch(BRANCH, base)
    proj.commit_to(
        BRANCH,
        {
            "docs/design/RULES.md": "Rule one.\nRule two, amended.\nRule three.\n",
            "assets/gfx/modify.png": png(b"after"),
            "assets/gfx/remove.png": None,
            "assets/gfx/added.png": added_content,
        },
    )
    gen = assert_reconstructs(proj, tmp_path)
    body = gen.diff_bytes.decode()
    assert "rename from" not in body and "rename to" not in body  # a real delete + a real add, not a rename
    assert body.count("GIT binary patch") == 3  # modify.png, remove.png, added.png; keep.png is unchanged
    assert "Rule two, amended." in body
    assert sorted(gen.manifest["working_diff"]["working_artifact_files"]) == [
        "assets/gfx/added.png",
        "assets/gfx/keep.png",
        "assets/gfx/modify.png",
    ]


def test_artifact_root_does_not_pull_in_a_symlink_or_oversized_file(proj: GitEnv, tmp_path):
    base = proj.commit_to(
        INTEGRATION, {MANIFEST_PATH: manifest_md(*BASE_LISTED, roots=("assets/gfx/",)), "assets/gfx/ok.png": png()}
    )
    proj.set_branch(BRANCH, base)
    git(proj.seed, "fetch", "--quiet", "origin")
    git(proj.seed, "checkout", "--quiet", "-B", INTEGRATION, f"origin/{INTEGRATION}")
    git(proj.seed, "reset", "--quiet", "--hard", f"origin/{INTEGRATION}")
    (proj.seed / "assets" / "gfx" / "huge.png").write_bytes(png() * 100000)
    git(proj.seed, "add", "-A")
    # A symlink tree entry via plumbing (no OS symlink needed, portable to Windows):
    # hash-object -w really writes the blob, so the later commit/write-tree can read it.
    link_sha = git(proj.seed, "hash-object", "-w", "--stdin", input=b"/etc/passwd")
    git(proj.seed, "update-index", "--add", "--cacheinfo", f"120000,{link_sha},assets/gfx/link.png")
    git(proj.seed, "commit", "--quiet", "-m", "artifact root with symlink and oversized file")
    git(proj.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{INTEGRATION}")
    git(proj.seed, "clean", "-fdq")
    proj.set_branch(BRANCH, proj.head(INTEGRATION))
    export = proj.repo_config.export.model_copy(update={"max_file_bytes": 1024})
    proj.with_repo_config(export=export)
    gen = generate(proj)
    assert gen.manifest["not_exported"]["assets/gfx/link.png"] == "symlink"
    assert gen.manifest["not_exported"]["assets/gfx/huge.png"] == "too_large"
    assert "assets/gfx/ok.png" in gen.manifest["files"]


def test_malformed_artifact_roots_in_manifest_fails(proj: GitEnv):
    proj.commit_to(BRANCH, {MANIFEST_PATH: manifest_md(*BASE_LISTED) + "\n<!-- PROJECT_ARTIFACT_ROOTS_BEGIN -->\n```yaml\nproject_artifact_roots: [../etc]\n```\n<!-- PROJECT_ARTIFACT_ROOTS_END -->\n"})
    with pytest.raises(SnapshotError, match="invalid project_artifact_roots"):
        generate(proj)


@pytest.mark.parametrize(
    "roots_yaml,problem",
    [
        ("project_artifact_roots: [../etc]", "invalid project_artifact_roots"),
        ("project_artifact_roots: [/abs/path]", "invalid project_artifact_roots"),
        ("project_artifact_roots: [assets/gfx/, assets/gfx]", "listed twice"),
        ("project_artifact_roots: not-a-list", "must be a list of strings"),
        ("not_the_right_key: [a/]", "with no project_artifact_roots"),
    ],
)
def test_malformed_artifact_roots_block_rejected(roots_yaml, problem):
    manifest = f"<!-- PROJECT_ARTIFACT_ROOTS_BEGIN -->\n{roots_yaml}\n<!-- PROJECT_ARTIFACT_ROOTS_END -->"
    with pytest.raises(SnapshotError, match=problem):
        parse_artifact_roots(manifest.encode(), "M.md")


def test_no_artifact_roots_block_is_not_an_error():
    assert parse_artifact_roots(b"# just a manifest, no PROJECT_ARTIFACT_ROOTS block\n", "M.md") == []


def test_unicode_paths_and_missing_trailing_newline(proj: GitEnv, tmp_path):
    listed = [*BASE_LISTED, "docs/人物/曹操.md"]
    proj.commit_to(BRANCH, {MANIFEST_PATH: manifest_md(*listed), "docs/人物/曹操.md": "曹操 without newline", "AGENTS.md": "no newline"})
    assert_reconstructs(proj, tmp_path)


def test_overlay_is_deterministic(proj: GitEnv):
    proj.commit_to(BRANCH, {"AGENTS.md": "changed\n"})
    first, second = generate(proj), generate(proj)
    assert first.diff_bytes.split(b"\n\n", 1)[1] == second.diff_bytes.split(b"\n\n", 1)[1]
    assert first.fingerprint == second.fingerprint
    assert first.generation_id != second.generation_id


def test_repository_file_named_snapshot_json_is_not_exported(proj: GitEnv, tmp_path):
    proj.commit_to(INTEGRATION, {MANIFEST_PATH: manifest_md(*BASE_LISTED, "snapshot.json"), "snapshot.json": "{}\n"})
    proj.set_branch(BRANCH, proj.head(INTEGRATION))
    gen = assert_reconstructs(proj, tmp_path)
    assert gen.manifest["not_exported"]["snapshot.json"] == "unsupported_path"
    assert unzip(gen.snapshot_zip)[0]["format_version"] == 2  # the real manifest


def test_missing_manifest_fails(proj: GitEnv):
    proj.commit_to(BRANCH, {MANIFEST_PATH: None})
    with pytest.raises(SnapshotError, match="does not exist"):
        generate(proj)


@pytest.mark.parametrize(
    "manifest,problem",
    [
        ("# no blocks here\n", "no PROJECT_SOURCE_FILES block"),
        ("<!-- PROJECT_SOURCE_FILES_BEGIN -->\nproject_source_files: [a.md, a.md]\n<!-- PROJECT_SOURCE_FILES_END -->", "listed twice"),
        ("<!-- PROJECT_SOURCE_FILES_BEGIN -->\nproject_source_files: [../etc/passwd]\n<!-- PROJECT_SOURCE_FILES_END -->", "unsafe path"),
        ("<!-- PROJECT_SOURCE_FILES_BEGIN -->\nproject_source_files: [[}\n<!-- PROJECT_SOURCE_FILES_END -->", "not valid YAML"),
        ("<!-- PROJECT_SOURCE_FILES_BEGIN -->\nproject_source_files: []\n<!-- PROJECT_SOURCE_FILES_END -->", "empty"),
    ],
)
def test_malformed_manifest_rejected(manifest, problem):
    with pytest.raises(SnapshotError, match=problem):
        parse_manifest(manifest.encode(), "M.md")

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import GitEnv
from git_bridge.api import create_app
from git_bridge.config import ServerConfig, ViewerConfig
from renderer_server.router import detect_content_kind

TOKEN = "viewer-t0k3n-" + "x" * 40


def _client(env: GitEnv) -> TestClient:
    token_file = env.tmp / "bearer-token"
    token_file.write_text(TOKEN + "\n")
    server = ServerConfig(bearer_token_file=token_file)
    viewer = ViewerConfig(storage_dir=env.tmp / "viewer-storage", base_url="https://renderer.test", max_upload_bytes=64)
    env.settings = env.settings.model_copy(update={"server": server, "viewer": viewer})
    return TestClient(create_app(env.settings, bridge=env.bridge))


@pytest.fixture
def client(gitenv: GitEnv) -> TestClient:
    return _client(gitenv)


def auth_headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------- mount


def test_viewer_section_is_optional(gitenv: GitEnv) -> None:
    """No viewer: config -> no /viewer/* routes, no effect on the rest of the app."""
    token_file = gitenv.tmp / "bearer-token"
    token_file.write_text(TOKEN + "\n")
    gitenv.settings = gitenv.settings.model_copy(update={"server": ServerConfig(bearer_token_file=token_file)})
    client = TestClient(create_app(gitenv.settings, bridge=gitenv.bridge))
    assert client.get("/viewer/health").status_code == 404
    assert client.get("/health").status_code == 200


def test_existing_routes_and_docs_still_disabled(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --------------------------------------------------------------------- auth


def test_import_rejects_missing_token(client: TestClient) -> None:
    response = client.post("/viewer/import", content="hello", headers={"Content-Type": "text/plain"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


def test_import_rejects_wrong_token(client: TestClient) -> None:
    response = client.post(
        "/viewer/import",
        content="hello",
        headers={**auth_headers("wrong-" + "y" * 40), "Content-Type": "text/plain"},
    )

    assert response.status_code == 401


def test_viewer_reads_require_no_token(client: TestClient) -> None:
    """GET routes stay open: they're meant to be opened as a plain link."""
    assert client.get("/viewer/render").status_code == 404  # no import yet, but not 401
    assert client.get("/viewer/api/content").status_code == 404
    assert client.get("/viewer/").status_code == 200


# ------------------------------------------------------------------- assets


@pytest.mark.parametrize(
    "path",
    [
        "/viewer/favicon.ico",
        "/viewer/apple-touch-icon.png",
        "/viewer/apple-touch-icon-precomposed.png",
    ],
)
def test_browser_icons_are_served(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_jsdiff_static_asset_is_served(client: TestClient) -> None:
    response = client.get("/viewer/static/jsdiff.min.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert b"diffWordsWithSpace" in response.content


def test_mathjax_static_asset_is_served(client: TestClient) -> None:
    response = client.get("/viewer/static/mathjax.min.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert b"MathJax" in response.content


# -------------------------------------------------------------------- import


def test_import_accepts_raw_text(client: TestClient) -> None:
    response = client.post(
        "/viewer/import",
        content="hello from shortcut",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    assert response.status_code == 200
    assert response.json() == {"url": "https://renderer.test/viewer/render"}


def test_import_accepts_json_content(client: TestClient) -> None:
    response = client.post(
        "/viewer/import",
        json={"content": "# Title", "filename": "../notes.md"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert "# Title" in client.get("/viewer/render").text


def test_import_accepts_multipart_file(client: TestClient) -> None:
    response = client.post(
        "/viewer/import",
        files={"file": ("changes.diff", b"--- a/file\n+++ b/file\n", "text/plain")},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert "--- a/file\\n+++ b/file\\n" in client.get("/viewer/render").text


def test_stored_content_can_be_rendered(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="diff --git a/a b/a",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "diff --git a/a b/a" in response.text


def test_next_import_overwrites_current_content(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="old-content-marker",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )
    client.post(
        "/viewer/import",
        content="new-content-marker",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    render = client.get("/viewer/render").text
    assert "new-content-marker" in render
    assert "old-content-marker" not in render


def test_api_content_and_meta_return_latest_import(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        json={"content": "latest", "filename": "latest.md"},
        headers=auth_headers(),
    )

    content = client.get("/viewer/api/content")
    metadata = client.get("/viewer/api/meta")

    assert content.status_code == 200
    assert content.text == "latest"
    assert content.headers["content-type"].startswith("text/plain")
    assert metadata.status_code == 200
    assert metadata.json()["filename"] == "latest.md"
    assert metadata.json()["updated_at"].endswith("Z")


def test_api_endpoints_return_404_before_first_import(client: TestClient) -> None:
    assert client.get("/viewer/api/content").status_code == 404
    assert client.get("/viewer/api/meta").status_code == 404


def test_render_page_opens_for_import(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="# Render me",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "Imported content" in response.text
    assert "__IMPORTED_CONTENT_JSON__" not in response.text


def test_render_returns_404_before_first_import(client: TestClient) -> None:
    response = client.get("/viewer/render")

    assert response.status_code == 404


def test_oversized_upload_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/viewer/import",
        content="x" * 65,
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    assert response.status_code == 413


def test_invalid_utf8_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/viewer/import",
        content=b"\xff\xfe\xff",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    assert response.status_code == 400


def test_path_traversal_attempts_do_not_work(client: TestClient) -> None:
    response = client.get("/viewer/api/content/../../AGENTS.md")

    assert response.status_code == 404


# ------------------------------------------------------ content-kind detection


def test_diff_with_markdown_content_is_detected_as_diff() -> None:
    text = """--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-# Old title
+# New title
"""

    assert detect_content_kind(text) == "diff"


def test_chatgpt_style_diff_command_is_detected_as_diff() -> None:
    text = """diff README.md
--- README.md
+++ README.md
@@ -1 +1 @@
-# Old title
+# New title
"""

    assert detect_content_kind(text) == "diff"


def test_bom_prefixed_diff_is_detected_as_diff() -> None:
    text = "﻿--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"

    assert detect_content_kind(text) == "diff"


def test_markdown_with_fenced_diff_block_is_detected_as_markdown() -> None:
    text = """# Release notes

```diff
--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-old
+new
```
"""

    assert detect_content_kind(text) == "markdown"


# --------------------------------------------------------------- render.html


def test_render_page_includes_markdown_table_renderer(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="| Name | Value |\n| --- | --- |\n| A | 1 |\n",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "function renderTable" in response.text
    assert 'const importedKind = "markdown";' in response.text


def test_render_page_includes_markdown_table_of_contents_renderer(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="# Intro\n\n## Details\n\n## Details\n",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "function renderTableOfContents" in response.text
    assert "function uniqueHeadingId" in response.text
    assert 'const importedKind = "markdown";' in response.text


def test_render_page_includes_local_math_renderer(client: TestClient) -> None:
    equation = r"A progress function \(\Delta_I(s) \geq 0\)."
    client.post(
        "/viewer/import",
        content=equation,
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert 'const importedKind = "markdown";' in response.text
    assert '<script defer src="/viewer/static/mathjax.min.js"></script>' in response.text
    assert 'inlineMath: { "[+]": [["$", "$"]] }' in response.text
    assert r"\\(\\Delta_I(s) \\geq 0\\)" in response.text


def test_render_embeds_detected_diff_kind(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# Old\n+# New\n",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert 'const importedKind = "diff";' in response.text


def test_render_embeds_detected_diff_kind_for_bom_prefixed_diff(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="﻿--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-Old\n+New\n",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert 'const importedKind = "diff";' in response.text
    assert "function stripLeadingBom" in response.text


def test_render_page_uses_readable_wrapping_diff_styles(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="--- a\n+++ b\n@@ -1 +1 @@\n+long line",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "font: 1rem/1.65 ui-monospace" in response.text
    assert "table-layout: fixed" in response.text
    assert "white-space: pre-wrap" in response.text
    assert "overflow-wrap: anywhere" in response.text


def test_render_page_includes_word_level_diff_renderer(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="--- a\n+++ b\n@@ -1 +1 @@\n-The old brown fox\n+The quick brown fox",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert '<script src="/viewer/static/jsdiff.min.js"></script>' in response.text
    assert "function jsDiffWords" in response.text
    assert "window.Diff.diffWordsWithSpace" in response.text
    assert "function diffWords" in response.text
    assert "function findWordChanges" in response.text
    assert "diff-word-add" in response.text
    assert "diff-word-del" in response.text


def test_word_diff_compares_whole_replacement_groups(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content=(
            "--- a\n+++ b\n@@ -1,2 +1,3 @@\n"
            "-B one\n-C one\n+A two\n+B two\n+C two"
        ),
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert 'deleted.map((lineIndex) => lines[lineIndex].slice(1)).join("\\n")' in response.text
    assert 'added.map((lineIndex) => lines[lineIndex].slice(1)).join("\\n")' in response.text
    assert "function assignWordChangesToLines" in response.text
    assert "lines[deleted[pair]]" not in response.text


def test_diff_render_includes_linked_file_table_of_contents(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content=(
            "diff a.py\n--- a.py\n+++ a.py\n"
            "diff b.py\n--- b.py\n+++ b.py"
        ),
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "function findDiffFileSections" in response.text
    assert "function renderDiffTableOfContents" in response.text
    assert 'nav.setAttribute("aria-label", "Changed files")' in response.text
    assert "section.id = fileSection.id" in response.text
    assert "diff-file-section:target" in response.text


def test_diff_render_has_sticky_file_headers_clear_of_safe_area(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="diff a.py\n--- a.py\n+++ a.py\n@@ -1 +1 @@\n-x\n+y",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "viewport-fit=cover" in response.text
    assert "function renderDiffFileHeader" in response.text
    assert "position: sticky;\n        top: env(safe-area-inset-top);" in response.text
    assert "overflow-x: clip;" in response.text


def test_diff_table_of_contents_counts_added_and_deleted_lines_per_file(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="diff a\n@@ -1 +1 @@\n-old\n+new",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "section.addedLines = 0" in response.text
    assert "section.deletedLines = 0" in response.text
    assert 'lineTypes[lineIndex] === "add"' in response.text
    assert 'lineTypes[lineIndex] === "del"' in response.text
    assert 'lineCount.className = "diff-toc-line-count"' in response.text
    assert '`+${section.addedLines}, -${section.deletedLines}`' in response.text


def test_diff_table_of_contents_ignores_wrapper_command_before_first_file(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content=(
            "diff a b\ndiff a/f b/f\n--- a/f\n+++ b/f\n"
            "@@ -1 +1 @@\n-x\n+y"
        ),
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "sectionAwaitingHeaders.lineIndex === lineIndex - 1" in response.text
    assert "sections.pop()" in response.text


def test_diff_row_styles_do_not_color_unchanged_words_as_changed(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="--- a\n+++ b\n@@ -1 +1 @@\n-The old brown fox\n+The quick brown fox",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert ".diff-add {\n        background: var(--add-bg);\n      }" in response.text
    assert ".diff-del {\n        background: var(--del-bg);\n      }" in response.text
    assert ".diff-word-add {\n        background: var(--word-add-bg);\n        color: CanvasText;\n      }" in response.text
    assert ".diff-word-del {\n        background: var(--word-del-bg);\n        color: CanvasText;\n      }" in response.text


def test_render_diff_classifies_hunk_marker_like_content_as_changed(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="--- a\n+++ b\n@@ -1 +1 @@\n---old\n+++new\n",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "function buildDiffLineTypes" in response.text
    assert 'lineTypes[index] === "add"' in response.text
    assert 'lineTypes[index] === "del"' in response.text


def test_render_embeds_content_without_closing_script_tag(client: TestClient) -> None:
    client.post(
        "/viewer/import",
        content="</script><script>alert('nope')</script>",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/viewer/render")

    assert response.status_code == 200
    assert "</script><script>" not in response.text
    assert "<\\/script><script>" in response.text

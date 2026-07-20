from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from renderer_server.app import create_app, detect_content_kind
from renderer_server.config import Settings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        base_url="https://renderer.test",
        import_token="test-token",
        storage_dir=tmp_path / "imports",
        max_upload_bytes=64,
        dev_http=True,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def auth_headers(token: str = "test-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_import_rejects_missing_token(client: TestClient) -> None:
    response = client.post("/import", content="hello", headers={"Content-Type": "text/plain"})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/favicon.ico",
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
    ],
)
def test_browser_icons_are_served(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_jsdiff_static_asset_is_served(client: TestClient) -> None:
    response = client.get("/static/jsdiff.min.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert b"diffWordsWithSpace" in response.content


def test_mathjax_static_asset_is_served(client: TestClient) -> None:
    response = client.get("/static/mathjax.min.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert b"MathJax" in response.content


def test_import_rejects_wrong_token(client: TestClient) -> None:
    response = client.post(
        "/import",
        content="hello",
        headers={**auth_headers("wrong"), "Content-Type": "text/plain"},
    )

    assert response.status_code == 401


def test_import_accepts_raw_text(client: TestClient) -> None:
    response = client.post(
        "/import",
        content="hello from shortcut",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "url": "https://renderer.test/render",
    }


def test_import_accepts_json_content(client: TestClient) -> None:
    response = client.post(
        "/import",
        json={"content": "# Title", "filename": "../notes.md"},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert "# Title" in client.get("/render").text


def test_import_accepts_multipart_file(client: TestClient) -> None:
    response = client.post(
        "/import",
        files={"file": ("changes.diff", b"--- a/file\n+++ b/file\n", "text/plain")},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert "--- a/file\\n+++ b/file\\n" in client.get("/render").text


def test_stored_content_can_be_rendered(client: TestClient) -> None:
    client.post(
        "/import",
        content="diff --git a/a b/a",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert "diff --git a/a b/a" in response.text


def test_next_import_overwrites_current_content(client: TestClient) -> None:
    client.post(
        "/import",
        content="old-content-marker",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )
    client.post(
        "/import",
        content="new-content-marker",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    render = client.get("/render").text
    assert "new-content-marker" in render
    assert "old-content-marker" not in render


def test_api_content_and_meta_return_latest_import(client: TestClient) -> None:
    client.post(
        "/import",
        json={"content": "latest", "filename": "latest.md"},
        headers=auth_headers(),
    )

    content = client.get("/api/content")
    metadata = client.get("/api/meta")

    assert content.status_code == 200
    assert content.text == "latest"
    assert content.headers["content-type"].startswith("text/plain")
    assert metadata.status_code == 200
    assert metadata.json()["filename"] == "latest.md"
    assert metadata.json()["updated_at"].endswith("Z")


def test_api_endpoints_return_404_before_first_import(client: TestClient) -> None:
    assert client.get("/api/content").status_code == 404
    assert client.get("/api/meta").status_code == 404


def test_render_page_opens_for_import(client: TestClient) -> None:
    client.post(
        "/import",
        content="# Render me",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert "Imported content" in response.text
    assert "__IMPORTED_CONTENT_JSON__" not in response.text


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
    text = "\ufeff--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"

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


def test_render_page_includes_markdown_table_renderer(client: TestClient) -> None:
    client.post(
        "/import",
        content="| Name | Value |\n| --- | --- |\n| A | 1 |\n",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert "function renderTable" in response.text
    assert 'const importedKind = "markdown";' in response.text


def test_render_page_includes_markdown_table_of_contents_renderer(client: TestClient) -> None:
    client.post(
        "/import",
        content="# Intro\n\n## Details\n\n## Details\n",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert "function renderTableOfContents" in response.text
    assert "function uniqueHeadingId" in response.text
    assert 'const importedKind = "markdown";' in response.text


def test_render_page_includes_local_math_renderer(client: TestClient) -> None:
    equation = r"A progress function \(\Delta_I(s) \geq 0\)."
    client.post(
        "/import",
        content=equation,
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert 'const importedKind = "markdown";' in response.text
    assert '<script defer src="/static/mathjax.min.js"></script>' in response.text
    assert 'inlineMath: { "[+]": [["$", "$"]] }' in response.text
    assert r"\\(\\Delta_I(s) \\geq 0\\)" in response.text


def test_render_embeds_detected_diff_kind(client: TestClient) -> None:
    client.post(
        "/import",
        content="--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# Old\n+# New\n",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert 'const importedKind = "diff";' in response.text


def test_render_embeds_detected_diff_kind_for_bom_prefixed_diff(client: TestClient) -> None:
    client.post(
        "/import",
        content="\ufeff--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-Old\n+New\n",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert 'const importedKind = "diff";' in response.text
    assert "function stripLeadingBom" in response.text


def test_render_page_uses_readable_wrapping_diff_styles(client: TestClient) -> None:
    client.post(
        "/import",
        content="--- a\n+++ b\n@@ -1 +1 @@\n+long line",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert "font: 1rem/1.65 ui-monospace" in response.text
    assert "table-layout: fixed" in response.text
    assert "white-space: pre-wrap" in response.text
    assert "overflow-wrap: anywhere" in response.text


def test_render_page_includes_word_level_diff_renderer(client: TestClient) -> None:
    client.post(
        "/import",
        content="--- a\n+++ b\n@@ -1 +1 @@\n-The old brown fox\n+The quick brown fox",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert '<script src="/static/jsdiff.min.js"></script>' in response.text
    assert "function jsDiffWords" in response.text
    assert "window.Diff.diffWordsWithSpace" in response.text
    assert "function diffWords" in response.text
    assert "function findWordChanges" in response.text
    assert "diff-word-add" in response.text
    assert "diff-word-del" in response.text


def test_word_diff_compares_whole_replacement_groups(client: TestClient) -> None:
    client.post(
        "/import",
        content=(
            "--- a\n+++ b\n@@ -1,2 +1,3 @@\n"
            "-B one\n-C one\n+A two\n+B two\n+C two"
        ),
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert 'deleted.map((lineIndex) => lines[lineIndex].slice(1)).join("\\n")' in response.text
    assert 'added.map((lineIndex) => lines[lineIndex].slice(1)).join("\\n")' in response.text
    assert "function assignWordChangesToLines" in response.text
    assert "lines[deleted[pair]]" not in response.text


def test_diff_render_includes_linked_file_table_of_contents(client: TestClient) -> None:
    client.post(
        "/import",
        content=(
            "diff a.py\n--- a.py\n+++ a.py\n"
            "diff b.py\n--- b.py\n+++ b.py"
        ),
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert "function findDiffFileSections" in response.text
    assert "function renderDiffTableOfContents" in response.text
    assert 'nav.setAttribute("aria-label", "Changed files")' in response.text
    assert "row.id = fileSection.id" in response.text
    assert "diff-file-anchor:target" in response.text


def test_diff_row_styles_do_not_color_unchanged_words_as_changed(client: TestClient) -> None:
    client.post(
        "/import",
        content="--- a\n+++ b\n@@ -1 +1 @@\n-The old brown fox\n+The quick brown fox",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert ".diff-add {\n        background: var(--add-bg);\n      }" in response.text
    assert ".diff-del {\n        background: var(--del-bg);\n      }" in response.text
    assert ".diff-word-add {\n        background: var(--word-add-bg);\n        color: CanvasText;\n      }" in response.text
    assert ".diff-word-del {\n        background: var(--word-del-bg);\n        color: CanvasText;\n      }" in response.text


def test_render_diff_classifies_hunk_marker_like_content_as_changed(client: TestClient) -> None:
    client.post(
        "/import",
        content="--- a\n+++ b\n@@ -1 +1 @@\n---old\n+++new\n",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert "function buildDiffLineTypes" in response.text
    assert 'lineTypes[index] === "add"' in response.text
    assert 'lineTypes[index] === "del"' in response.text


def test_render_embeds_content_without_closing_script_tag(client: TestClient) -> None:
    client.post(
        "/import",
        content="</script><script>alert('nope')</script>",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    response = client.get("/render")

    assert response.status_code == 200
    assert "</script><script>" not in response.text
    assert "<\\/script><script>" in response.text


def test_render_returns_404_before_first_import(client: TestClient) -> None:
    response = client.get("/render")

    assert response.status_code == 404


def test_oversized_upload_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/import",
        content="x" * 65,
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    assert response.status_code == 413


def test_invalid_utf8_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/import",
        content=b"\xff\xfe\xff",
        headers={**auth_headers(), "Content-Type": "text/plain"},
    )

    assert response.status_code == 400


def test_path_traversal_attempts_do_not_work(client: TestClient) -> None:
    response = client.get("/api/content/../../AGENTS.md")

    assert response.status_code == 404

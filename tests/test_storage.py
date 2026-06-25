from datetime import UTC, datetime
from pathlib import Path

from renderer_server.storage import FileStorage


def test_save_uses_updated_at_metadata(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    stored = storage.save("hello", filename="note.md", created_at=now)

    assert stored.metadata["updated_at"] == "2026-06-20T12:00:00Z"
    assert stored.metadata["size_bytes"] == 5

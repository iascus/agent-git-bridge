from datetime import UTC, datetime, timedelta
from pathlib import Path

from renderer_server.storage import FileStorage, cleanup_expired_imports


def test_cleanup_removes_old_imports_but_keeps_recent(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    old = storage.save("old", created_at=now - timedelta(hours=25))

    removed = cleanup_expired_imports(tmp_path, 24, now=now)

    assert removed == 1
    assert not old.content_path.exists()


def test_cleanup_keeps_recent_import(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    recent = storage.save("recent", created_at=now - timedelta(hours=1))

    removed = cleanup_expired_imports(tmp_path, 24, now=now)

    assert removed == 0
    assert recent.content_path.exists()

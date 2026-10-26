"""Database backup: a consistent copy of huthop.db in data/backups/, one file per day.

    uv run python -m app.backup                                          # on the Mac
    docker compose exec -T huthop python -m app.backup                   # on the server, in /opt/huthop/server

Uses SQLite's backup API, so it is safe while the app is running and writing. Keeps the newest KEEP copies.
"""

import sqlite3
from datetime import date
from pathlib import Path

from app.db import DB_PATH

KEEP = 14


def backup(db_path: Path = DB_PATH, backup_dir: Path | None = None, keep: int = KEEP,
           day: date | None = None) -> Path:
    backup_dir = backup_dir or db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"huthop-{(day or date.today()).isoformat()}.db"
    partial = target.with_suffix(".partial")
    src, dst = sqlite3.connect(db_path), sqlite3.connect(partial)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    partial.replace(target)  # a crash mid-copy never leaves a broken file under a backup name
    for old in sorted(backup_dir.glob("huthop-*.db"))[:-keep]:
        old.unlink()
    return target


if __name__ == "__main__":
    print(f"saved {backup()}")

"""SQLite access. The schema is applied once (at app startup, or by jobs and tests via `init=True`)."""

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("HUTHOP_DB", ROOT / "data/huthop.db"))
SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: Path | str = DB_PATH, init: bool = True) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if init:
        conn.executescript(SCHEMA.read_text())
        # Readers never wait for a writer (availability cache, edits). Stored in the file, so set once is enough.
        conn.execute("PRAGMA journal_mode = WAL")
    return conn

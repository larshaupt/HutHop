"""SQLite access. The schema is applied once (at app startup, or by jobs and tests via `init=True`)."""

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("HUTHOP_DB", ROOT / "data/huthop.db"))
SCHEMA = Path(__file__).with_name("schema.sql")
# Columns added after the first deploy: (table, column, type). New databases get them from schema.sql.
ADDED_COLUMNS = [("edge", "elevation", "TEXT"), ("edge", "footprint", "BLOB"), ("edge", "samples", "BLOB"),
                 ("hut", "phone", "TEXT"), ("hut", "email", "TEXT"), ("hut_web", "email", "TEXT"),
                 ("edge", "max_altitude_m", "INTEGER")]


def connect(path: Path | str = DB_PATH, init: bool = True) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # The refresh holds the write lock for long stretches (timetable, edges); app writes wait instead of failing
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if init:
        for table, column, sql_type in ADDED_COLUMNS:
            columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if columns and column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        # hut_price before website prices: never filled, so replaced by the new layout while it is empty.
        price_columns = {r["name"] for r in conn.execute("PRAGMA table_info(hut_price)")}
        if price_columns and "label" not in price_columns and not conn.execute("SELECT 1 FROM hut_price").fetchone():
            conn.execute("DROP TABLE hut_price")
        conn.executescript(SCHEMA.read_text())
        # Readers never wait for a writer (availability cache, edits). Stored in the file, so set once is enough.
        conn.execute("PRAGMA journal_mode = WAL")
    return conn

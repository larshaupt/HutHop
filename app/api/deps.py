"""Shared FastAPI dependencies."""

from app.db import connect


def get_db():
    """One connection per request; the schema was already applied at startup."""
    conn = connect(init=False)
    try:
        yield conn
    finally:
        conn.close()

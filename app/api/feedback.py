"""Feedback from the app: anyone who can use it can send some; reading it needs the admin password."""

import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.admin import require_admin
from app.api.deps import get_db

router = APIRouter(prefix="/api/feedback", tags=["feedback"])
EMAIL_HEADER = "cf-access-authenticated-user-email"  # set by Cloudflare Access


class Feedback(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    page: str | None = Field(None, max_length=1000)


@router.post("")
def send_feedback(f: Feedback, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    with conn:
        conn.execute("INSERT INTO feedback (created_at, message, page, email) VALUES (?, ?, ?, ?)",
                     (datetime.now(UTC).isoformat(timespec="seconds"), f.message.strip(), f.page,
                      request.headers.get(EMAIL_HEADER)))
    return {"ok": True}


@router.get("", dependencies=[Depends(require_admin)])
def list_feedback(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    """All feedback, newest first (open /api/feedback in the browser and log in with the admin password)."""
    return [dict(r) for r in conn.execute("SELECT * FROM feedback ORDER BY id DESC")]

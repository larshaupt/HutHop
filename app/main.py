"""HutHop web app: JSON API under /api, static frontend at /.

    uv run uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

import sqlite3

from fastapi import Depends, FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import edges, huts, manual, tours
from app.api.deps import get_db
from app.db import ROOT, connect


@asynccontextmanager
async def lifespan(_app: FastAPI):
    connect().close()  # create tables and views once
    yield


app = FastAPI(title="HutHop", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(manual.router)
app.include_router(huts.router)
app.include_router(edges.router)
app.include_router(tours.router)


@app.get("/api/health", tags=["health"])
def health(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """For the Docker health check: the app answers and the database can be read."""
    hut_count, edge_count = (conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("hut", "edge"))
    return {"status": "ok", "huts": hut_count, "edges": edge_count}


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")

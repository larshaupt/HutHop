"""HutHop web app: JSON API under /api, static frontend at /.

    uv run uvicorn app.main:app --reload
"""

import logging
import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import edges, feedback, huts, info, manual, tours
from app.api.deps import get_db
from app.db import ROOT, connect
from app.planner import graph

WARM_CHECK_S = 60  # how often the warm-up thread looks for changed data (e.g. after a refresh)


def keep_warm(stop: threading.Event) -> None:
    """Load the route graph and footprints at start-up and again after the data changed, so no search waits."""
    conn = connect(init=False)
    while not stop.is_set():
        try:
            if graph.warm_up(conn):
                logging.getLogger("uvicorn.error").info("route graph loaded")
        except Exception:  # e.g. a database being replaced; try again later
            logging.getLogger("uvicorn.error").exception("route graph warm-up failed")
        stop.wait(WARM_CHECK_S)
    conn.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    connect().close()  # create tables and views once
    stop = threading.Event()
    threading.Thread(target=keep_warm, args=(stop,), name="warm-up", daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="HutHop", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(manual.router)
app.include_router(huts.router)
app.include_router(edges.router)
app.include_router(tours.router)
app.include_router(tours.pages)
app.include_router(info.router)
app.include_router(feedback.router)


@app.get("/api/health", tags=["health"])
def health(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """For the Docker health check: the app answers and the database can be read."""
    hut_count, edge_count = (conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("hut", "edge"))
    return {"status": "ok", "huts": hut_count, "edges": edge_count}


class Frontend(StaticFiles):
    """The frontend files, revalidated on every load (cheap: 304 via ETag), so a deploy is never hidden behind a
    stale browser cache."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", Frontend(directory=ROOT / "web", html=True), name="web")

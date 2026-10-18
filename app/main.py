"""HutHop web app: JSON API under /api, static frontend at /.

    uv run uvicorn app.main:app --reload
"""

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import edges, huts, tours
from app.db import ROOT

app = FastAPI(title="HutHop")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(huts.router)
app.include_router(edges.router)
app.include_router(tours.router)
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")

# HutHop web app: FastAPI + static frontend. The SQLite database lives in /app/data (a volume).
# Ingest jobs run from the same image in a one-off container (server/refresh.sh); see docs/deployment.md.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first, so code changes don't reinstall them
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY web ./web
COPY huthop.toml ./

ENV PATH="/app/.venv/bin:$PATH" \
    HUTHOP_DB=/app/data/huthop.db

# Runs as uid 1000, the usual first user on a Pi or Linux box. ./data on the host must be writable by it.
RUN useradd --uid 1000 --create-home huthop && mkdir -p /app/data && chown huthop /app/data
USER huthop

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

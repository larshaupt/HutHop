"""Minimal Gemini client on Vertex AI (generateContent with a JSON response schema). Used by
app/ingest/scrape_huts.py.

Authenticates with the gcloud CLI (`gcloud auth login`); no API key is stored. The Google Cloud project is
gcloud's default project unless GEMINI_PROJECT is set. Gemini 3.5 Flash is served only from the `global`
location (Oct 2026).
"""

import base64
import json
import os
import subprocess
import threading
import time

import httpx

DEFAULT_MODEL = "gemini-3.5-flash"
LOCATION = "global"
RETRIES = 4
TOKEN_TTL_S = 45 * 60  # gcloud access tokens last an hour

_token: tuple[str, float] | None = None
_token_lock = threading.Lock()


def _gcloud(*args: str) -> str:
    try:
        return subprocess.run(["gcloud", *args], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"gcloud {' '.join(args)} failed; run `gcloud auth login` first ({e})") from None


def access_token() -> str:
    global _token
    with _token_lock:
        if not _token or time.monotonic() - _token[1] > TOKEN_TTL_S:
            _token = (_gcloud("auth", "print-access-token"), time.monotonic())
        return _token[0]


def project() -> str:
    return os.environ.get("GEMINI_PROJECT") or _gcloud("config", "get-value", "project")


def endpoint(model: str) -> str:
    host = "aiplatform.googleapis.com" if LOCATION == "global" else f"{LOCATION}-aiplatform.googleapis.com"
    return (f"https://{host}/v1/projects/{project()}/locations/{LOCATION}/publishers/google/models/"
            f"{model}:generateContent")


def generate_json(prompt: str, schema: dict, pdfs: list[bytes] = (), model: str = DEFAULT_MODEL,
                  client: httpx.Client | None = None) -> tuple[dict, dict]:
    """(parsed JSON answer, usage/model info). Retries on rate limits and server errors."""
    client = client or httpx.Client(timeout=300)
    parts = [{"text": prompt}] + [{"inlineData": {"mimeType": "application/pdf",
                                                  "data": base64.b64encode(p).decode()}} for p in pdfs]
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                 "temperature": 0}}
    url = endpoint(model)
    for attempt in range(RETRIES):
        r = client.post(url, json=body, headers={"Authorization": f"Bearer {access_token()}"})
        if r.status_code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
            time.sleep(10 * 2 ** attempt)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        candidate = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))
        if not text:
            raise RuntimeError(f"Gemini returned no text (finish reason {candidate.get('finishReason')})")
        info = {"model": data.get("modelVersion", model), **{k: v for k, v in (data.get("usageMetadata") or {}).items()
                                                            if k in ("promptTokenCount", "candidatesTokenCount")}}
        return json.loads(text), info
    raise RuntimeError("Gemini: too many retries")

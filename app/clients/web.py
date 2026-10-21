"""Polite fetching of hut websites: robots.txt, one request at a time per host, plain text out of HTML.

Used by app/ingest/scrape_huts.py. Only public pages, no forms, no login.
"""

import re
import threading
import time
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

USER_AGENT = "HutHop/0.1 (private hut planner)"
TIMEOUT_S = 20
HOST_DELAY_S = 2.0          # pause between two requests to the same host
MAX_BYTES = 8_000_000       # skip bigger responses (photo galleries, brochures)
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table",
              "ul", "ol", "header", "footer", "dt", "dd", "nav"}  # table cells stay on their row's line


class _Page(HTMLParser):
    """Collects visible text (one line per block element) and links with their text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = [""]
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._link: list | None = None

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            self._link = [href, ""] if href else None
        if tag in BLOCK_TAGS:
            self.lines.append("")
        if tag in ("td", "th"):
            self.lines[-1] += " | "

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "a":
            if self._link:
                self.links.append((self._link[0], self._link[1].strip()))
            self._link = None
            if not self._skip:
                self.lines[-1] += " "  # menu links written without spaces between them
        if tag in BLOCK_TAGS:
            self.lines.append("")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self.lines[-1] += data
        if self._link is not None:
            self._link[1] += data

    def text(self) -> str:
        lines = (re.sub(r"\s+", " ", line).strip(" |") for line in self.lines)
        out: list[str] = []
        for line in lines:
            if line and (not out or out[-1] != line):
                out.append(line)
        return "\n".join(out)


def parse_html(html: str, base_url: str) -> dict:
    """Title, visible text and absolute links (without #fragments) of an HTML page."""
    page = _Page()
    try:
        page.feed(html)
    except Exception:  # broken markup: keep what was parsed
        pass
    links = []
    for href, label in page.links:
        if href.startswith(("mailto:", "tel:", "javascript:")):
            links.append((href, label))
            continue
        url = urldefrag(urljoin(base_url, href)).url
        if url.startswith(("http://", "https://")):
            links.append((url, label))
    return {"title": re.sub(r"\s+", " ", page.title).strip(), "text": page.text(), "links": links}


def site(url: str) -> str:
    """Registered site of a URL for "same website" checks: host without www."""
    return (urlsplit(url).hostname or "").removeprefix("www.")


class Fetcher:
    """Thread-safe fetcher: respects robots.txt and waits HOST_DELAY_S between requests to one host."""

    def __init__(self) -> None:
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Language": "de,fr,it,en"},
                                   timeout=TIMEOUT_S, follow_redirects=True)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def _get(self, url: str) -> httpx.Response:
        host = urlsplit(url).netloc
        with self._host_lock(host):
            wait = self._last.get(host, 0) + HOST_DELAY_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                with self.client.stream("GET", url) as r:
                    if int(r.headers.get("content-length") or 0) > MAX_BYTES:
                        raise httpx.HTTPError(f"too large: {r.headers['content-length']} bytes")
                    body = b""
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise httpx.HTTPError("too large")
                    r._content = body
                    return r
            finally:
                self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                r = self._get(origin + "/robots.txt")
                parser.parse(r.text.splitlines() if r.status_code == 200 else [])
            except (httpx.HTTPError, UnicodeDecodeError):
                parser.parse([])
            self._robots[origin] = parser
        return self._robots[origin].can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> dict:
        """{url, final_url, status, kind: 'html'|'pdf'|'other', content (bytes)} or {url, error}."""
        if not self.allowed(url):
            return {"url": url, "error": "disallowed by robots.txt"}
        try:
            r = self._get(url)
        except httpx.HTTPError as e:
            return {"url": url, "error": f"{type(e).__name__}: {e}"[:300]}
        ctype = r.headers.get("content-type", "").lower()
        kind = "pdf" if "pdf" in ctype or r.content[:5] == b"%PDF-" else "html" if "html" in ctype else "other"
        return {"url": url, "final_url": str(r.url), "status": r.status_code, "kind": kind, "content": r.content,
                "encoding": r.encoding}

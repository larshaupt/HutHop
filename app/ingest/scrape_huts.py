"""Hut websites → season dates, prices, booking and contact details. Three stages, each can run alone:

    uv run python -m app.ingest.scrape_huts fetch [--ids 12,34] [--limit 20] [--refetch]
    uv run python -m app.ingest.scrape_huts extract [--ids ...] [--limit ...] [--model gemini-3.5-flash]
    uv run python -m app.ingest.scrape_huts load

fetch:   the hut's website from the SAC data, plus the linked pages most likely to hold prices, opening times or
         booking details (and price-list PDFs). Raw pages go to data/raw/web/<sac_id>.json (+ pdf/).
extract: Gemini reads the pages and returns a fixed JSON format (prompt and schema below), saved as
         data/raw/web/<sac_id>.extract.json. Uses Vertex AI through the gcloud login (app/clients/gemini.py).
load:    extracted values into hut_web, hut_season and hut_price (source 'scraped'). Manual overrides win.

Run once or twice per season; hut websites change rarely.
"""

import argparse
import hashlib
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import urlsplit

from app.clients import gemini, web
from app.db import ROOT, connect

RAW_DIR = ROOT / "data/raw/web"
PDF_DIR = RAW_DIR / "pdf"
MAX_SUBPAGES = 6
MAX_PDFS = 3
WORKERS = 8
# Words in a link's URL or text that point to useful pages (de, fr, it, en), with a weight.
LINK_WORDS = {
    3: ("preis", "tarif", "prix", "prezz", "price", "kosten", "taxe", "öffnung", "oeffnung", "offnung",
        "ouverture", "apertura", "opening", "saison", "season", "stagion", "reserv", "prenot", "buchen", "booking"),
    2: ("bewart", "gardienn", "custod", "winterraum", "local-d-hiver", "locale-invernale", "übernacht",
        "uebernacht", "nuitée", "pernott", "kontakt", "contact", "contatt", "info"),
    1: ("hütte", "huette", "hutte", "cabane", "capanna", "rifugio", "hut", "unterkunft", "angebot", "allgemein"),
}
SKIP_LINK = re.compile(r"\.(jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp4|ics)(\?|$)|/(wp-content/uploads/.*\.(?!pdf))"
                       r"|facebook\.com|instagram\.com|twitter\.com|youtube\.com|google\.|/login|/admin", re.I)
# External booking systems recognised in links (name → host part)
BOOKING_HOSTS = {"hut-reservation.org": "hut-reservation.org", "alpsonline": "alpsonline.org",
                 "bookingwidget": "booking", "simplybook": "simplybook", "e-guma": "e-guma", "lodgify": "lodgify",
                 "rifugi-online": "rifugi", "seekda": "seekda", "tomas": "tomas", "deskline": "deskline"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def link_score(url: str, label: str, base_site: str) -> int:
    """How promising a link looks; 0 = don't follow."""
    if SKIP_LINK.search(url) or url.startswith(("mailto:", "tel:")):
        return 0
    is_pdf = url.lower().split("?")[0].endswith(".pdf")
    if web.site(url) != base_site and not is_pdf:
        return 0
    parts = urlsplit(url)
    words = f"{parts.path} {parts.query} {label}".lower()  # not the host: huette.ch would match "huette"
    return sum(w for w, keys in LINK_WORDS.items() for k in keys if k in words)


def is_pdf_link(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def fetch_hut(fetcher: web.Fetcher, hut: dict) -> dict:
    """Start page, then best-first the most promising linked pages of the same site (MAX_SUBPAGES) and PDFs
    (MAX_PDFS). Contact links (tel:, mailto:) and links to known booking systems are kept separately."""
    out = {"sac_id": hut["sac_id"], "name": hut["name"], "website": hut["website"], "fetched_at": _now(),
           "pages": [], "pdfs": [], "contacts": [], "booking_links": [], "errors": []}
    candidates: dict[str, int] = {}
    seen: set[str] = set()
    base_site = None

    def add_page(res: dict) -> None:
        html = res["content"].decode(res["encoding"] or "utf-8", errors="replace")
        page = web.parse_html(html, res["final_url"])
        out["pages"].append({"url": res["final_url"], "title": page["title"], "text": page["text"]})
        seen.add(res["final_url"])
        for url, label in page["links"]:
            if url.startswith(("tel:", "mailto:")):
                if [url, label] not in out["contacts"]:
                    out["contacts"].append([url, label])
                continue
            host = urlsplit(url).hostname or ""
            if any(h in host for h in BOOKING_HOSTS.values()) and url not in out["booking_links"]:
                out["booking_links"].append(url)
            if score := link_score(url, label, base_site):
                candidates[url] = max(candidates.get(url, 0), score)

    def add_pdf(res: dict) -> None:
        digest = hashlib.sha256(res["content"]).hexdigest()[:16]
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        (PDF_DIR / f"{digest}.pdf").write_bytes(res["content"])
        out["pdfs"].append({"url": res["url"], "file": f"pdf/{digest}.pdf", "bytes": len(res["content"])})

    def get(url: str) -> dict | None:
        seen.add(url)
        res = fetcher.fetch(url)
        if "error" in res or res["status"] >= 400:
            out["errors"].append({"url": url, "error": res.get("error") or f"HTTP {res['status']}"})
            return None
        return res

    start = get(hut["website"])
    if not start or start["kind"] != "html":
        if start:
            out["errors"].append({"url": hut["website"], "error": f"start page is {start['kind']}"})
        return out
    base_site = web.site(start["final_url"])
    add_page(start)
    while len(out["pages"]) <= MAX_SUBPAGES or len(out["pdfs"]) < MAX_PDFS:
        pages_full = len(out["pages"]) > MAX_SUBPAGES
        pdfs_full = len(out["pdfs"]) >= MAX_PDFS
        best = max(((s, u) for u, s in candidates.items() if u not in seen
                    and not (pages_full and not is_pdf_link(u)) and not (pdfs_full and is_pdf_link(u))
                    and (s >= 3 or not is_pdf_link(u))), default=None)
        if not best:
            break
        res = get(best[1])
        if not res:
            continue
        if res["kind"] == "pdf" and not pdfs_full:
            add_pdf(res)
        elif res["kind"] == "html" and not pages_full and web.site(res["final_url"]) == base_site \
                and res["final_url"] not in {p["url"] for p in out["pages"]}:
            add_page(res)
    return out


def huts_to_scrape(conn: sqlite3.Connection, ids: list[int] | None, limit: int | None) -> list[dict]:
    """Huts with a website, by SAC id. Huts without online booking first: they gain the most."""
    rows = conn.execute("SELECT sac_id, name, website, hrs_id FROM hut_effective "
                        "WHERE website IS NOT NULL AND sac_id IS NOT NULL ORDER BY hrs_id IS NOT NULL, name")
    huts = [dict(r) for r in rows if not ids or r["sac_id"] in ids]
    return huts[:limit] if limit else huts


def fetch(huts: list[dict], refetch: bool = False) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    todo = [h for h in huts if refetch or not (RAW_DIR / f"{h['sac_id']}.json").exists()]
    fetcher = web.Fetcher()

    def one(hut: dict) -> str:
        try:
            result = fetch_hut(fetcher, hut)
        except Exception as e:  # one broken site must not stop the run
            result = {"sac_id": hut["sac_id"], "name": hut["name"], "website": hut["website"], "fetched_at": _now(),
                      "pages": [], "pdfs": [], "contacts": [], "booking_links": [],
                      "errors": [{"url": hut["website"], "error": f"{type(e).__name__}: {e}"[:300]}]}
        (RAW_DIR / f"{hut['sac_id']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
        chars = sum(len(p["text"]) for p in result["pages"])
        return (f"{hut['name'][:40]:40} pages {len(result['pages'])}  pdfs {len(result['pdfs'])}  "
                f"text {chars:>6}  errors {len(result['errors'])}")

    print(f"fetching {len(todo)} of {len(huts)} huts")
    with ThreadPoolExecutor(WORKERS) as pool:
        for i, line in enumerate(pool.map(one, todo), 1):
            print(f"{i:>4}/{len(todo)}  {line}", flush=True)


MAX_PAGE_CHARS = 20_000
MAX_PROMPT_CHARS = 90_000
PRICE_ITEMS = ["overnight", "half_board", "breakfast", "dinner", "tax", "winter_room", "self_catering_fee"]
_STR, _INT, _NUM, _BOOL = ({"type": t, "nullable": True} for t in ("STRING", "INTEGER", "NUMBER", "BOOLEAN"))
EXTRACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "about_this_hut": {"type": "BOOLEAN"},
        "phone": _STR,
        "email": _STR,
        "booking_methods": {"type": "ARRAY", "items": {"type": "STRING", "enum": [
            "hut_reservation_org", "online_other", "email", "phone", "not_needed"]}},
        "booking_url": _STR,
        "seasons": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "kind": {"type": "STRING", "enum": ["catered", "self_service", "closed"]},
            "start": {"type": "STRING"}, "end": {"type": "STRING"}, "year": _INT,
            "approximate": {"type": "BOOLEAN"}, "note": _STR, "quote": {"type": "STRING"},
            "source_url": {"type": "STRING"}},
            "required": ["kind", "start", "end", "year", "approximate", "quote", "source_url"]}},
        "closed_otherwise": _BOOL,
        "winter_room": _BOOL,
        "winter_room_beds": _INT,
        "beds": _INT,
        "prices": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "item": {"type": "STRING", "enum": PRICE_ITEMS},
            "label": {"type": "STRING"}, "includes_overnight": {"type": "BOOLEAN"},
            "member": _NUM, "non_member": _NUM, "youth": _NUM, "child": _NUM,
            "currency": {"type": "STRING", "enum": ["CHF", "EUR"]}, "year": _INT,
            "quote": {"type": "STRING"}, "source_url": {"type": "STRING"}},
            "required": ["item", "label", "includes_overnight", "member", "non_member", "currency", "year",
                         "quote", "source_url"]}},
        "notes": _STR,
    },
    "required": ["about_this_hut", "phone", "email", "booking_methods", "booking_url", "seasons", "closed_otherwise",
                 "winter_room", "winter_room_beds", "beds", "prices", "notes"],
}
PROMPT = """You extract facts about one mountain hut from its website, for a hut-to-hut hiking planner.
Use only what the pages below (and attached PDFs) say. Never guess; use null or an empty list when not stated.
Today is {today}.

Hut: {name} (SAC hut portal id {sac_id}), {altitude} m. Website: {website}

Fields:
- about_this_hut: false if the pages are not about this hut (dead page, generic tourism or club site without
  details on this hut). Then leave everything else empty.
- phone: number to call the hut or its reservations, in international format (+41 …). Prefer the hut / reservation
  number over a private or club office number.
- email: address to write to the hut or its reservations (same preference as phone).
- booking_methods: how to reserve a night (hut_reservation_org = hut-reservation.org; online_other = any other
  online form or booking system; not_needed = no reservation needed). booking_url: the reservation page or form.
- seasons: periods when the hut is open. kind "catered" = staffed/serviced (bewartet, gardiennée, custodita);
  "self_service" = unstaffed but usable (winter room, Winterraum, local d'hiver, locale invernale, self-catering).
  start/end as MM-DD. year: the year the dates are for, or null if stated as every year. approximate: true for
  vague dates ("mid June", "Ende September", "depending on snow"; use the 15th / last day of the month).
  Weekend-only service: kind "catered" with a note. quote: the sentence it comes from, verbatim.
  Only the most recent year's dates if several years are listed. Don't add a season for "on request" only.
  Give the whole season even if part of it is over ("open until 15 November" in a season that started in June:
  use the published start, or the 1st of the month with approximate true if none).
  kind "closed" = an exceptional closure of a hut that is normally open (renovation, damage, landslide, "no
  overnight stays in 2026"), with year and exact or approximate dates. Not for the regular off-season.
- closed_otherwise: true only if the site says the hut is closed / not accessible outside the listed seasons.
- winter_room: true / false if the site says whether a winter room or emergency shelter exists, else null.
- prices: per person per night, most recent year only. item: overnight (bed only), half_board (dinner +
  breakfast; includes_overnight true if the price includes the bed), breakfast, dinner, tax (tourist tax,
  Kurtaxe), winter_room (night in the winter room), self_catering_fee (Kochgebühr, use of the kitchen).
  member = Alpine club member (SAC/CAS/DAV/ÖAV/…) price; non_member = regular price; if there is only one
  price, put it in both. youth / child: reduced prices for young people and children, in the same entry as
  the adult price (never a separate entry; give the ages in label). includes_overnight: only for half_board.
  label: the original wording incl. room type (e.g. "Massenlager", "Doppelzimmer"). One entry per room type
  and item, at most 12; dormitory first. Skip meals à la carte, drinks, packed lunches, group packages, and
  prices only for local residents or for members of one particular club section.
- notes: up to 2 short sentences in English with practical facts a hiker must know before booking
  (e.g. reservation compulsory, cash only, no showers, dogs not allowed). null if nothing notable.

Pages:
{pages}
"""


def extract_hut(raw: dict, altitude: int | None, model: str) -> dict:
    """Ask Gemini for the fixed format; returns {result, info, extracted_at}."""
    budget = MAX_PROMPT_CHARS
    parts = []
    for page in raw["pages"]:
        text = page["text"][:min(MAX_PAGE_CHARS, budget)]
        if not text:
            break
        parts.append(f"--- {page['url']} ({page['title']})\n{text}")
        budget -= len(text)
    contacts = "\n".join(f"{url} {label}" for url, label in raw["contacts"][:20])
    if contacts:
        parts.append(f"--- contact links on the site\n{contacts}")
    if raw["booking_links"]:
        parts.append("--- booking system links on the site\n" + "\n".join(raw["booking_links"][:10]))
    for pdf in raw["pdfs"]:
        parts.append(f"--- attached PDF: {pdf['url']}")
    prompt = PROMPT.format(today=datetime.now(UTC).date().isoformat(), name=raw["name"], sac_id=raw["sac_id"],
                           altitude=altitude or "?", website=raw["website"], pages="\n\n".join(parts))
    pdfs = [(RAW_DIR / p["file"]).read_bytes() for p in raw["pdfs"]]
    result, info = gemini.generate_json(prompt, EXTRACT_SCHEMA, pdfs, model=model)
    return {"result": result, "info": info, "extracted_at": _now()}


def extract(conn: sqlite3.Connection, huts: list[dict], model: str, again: bool = False) -> None:
    altitudes = {r["sac_id"]: r["altitude_m"] for r in conn.execute("SELECT sac_id, altitude_m FROM hut_effective")}
    todo = []
    for h in huts:
        raw_file, out_file = RAW_DIR / f"{h['sac_id']}.json", RAW_DIR / f"{h['sac_id']}.extract.json"
        if raw_file.exists() and (again or not out_file.exists()):
            todo.append((h, json.loads(raw_file.read_text()), out_file))
    print(f"extracting {len(todo)} of {len(huts)} huts with {model}")

    def one(job) -> str:
        hut, raw, out_file = job
        if not raw["pages"]:
            out = {"result": None, "info": {}, "extracted_at": _now(), "error": "website unreachable"}
        else:
            try:
                out = extract_hut(raw, altitudes.get(hut["sac_id"]), model)
            except Exception as e:  # keep going; the hut is retried on the next run
                return f"{hut['name'][:40]:40} ERROR {e}"[:300]
        out_file.write_text(json.dumps(out, ensure_ascii=False, indent=1))
        r = out["result"] or {}
        return (f"{hut['name'][:40]:40} about {str(r.get('about_this_hut', '-')):5}  seasons "
                f"{len(r.get('seasons') or [])}  prices {len(r.get('prices') or [])}  "
                f"tokens {out['info'].get('promptTokenCount', 0)}")

    with ThreadPoolExecutor(4) as pool:
        for i, line in enumerate(pool.map(one, todo), 1):
            print(f"{i:>4}/{len(todo)}  {line}", flush=True)


def _month_day(value: str) -> str | None:
    """'MM-DD' if it is a real day of the year, else None."""
    try:
        return datetime.strptime(f"2024-{value}", "%Y-%m-%d").strftime("%m-%d")
    except (TypeError, ValueError):
        return None


def _words(text: str | None) -> str:
    """Lowercase words only, for comparing quotes with page text regardless of punctuation and line breaks."""
    return " ".join(re.findall(r"\w+", (text or "").lower()))


def _clean(text: str | None) -> str | None:
    """The model sometimes writes NUL instead of an accented letter; drop control characters."""
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", text).strip() or None if text else None


def _email(value: str | None) -> str | None:
    value = (value or "").strip().removeprefix("mailto:")
    return value if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) else None


def _price(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and 0 < value < 1000 else None


def load(conn: sqlite3.Connection) -> dict:
    """All saved extractions into hut_web, hut_season and hut_price (scraped rows of a hut are replaced)."""
    place_ids = {r["sac_id"]: r["place_id"] for r in conn.execute("SELECT sac_id, place_id FROM hut")}
    counts = {"huts": 0, "ok": 0, "seasons": 0, "prices": 0, "skipped_values": 0}
    now = _now()
    with conn:
        for out_file in sorted(RAW_DIR.glob("*.extract.json")):
            sac_id = int(out_file.name.split(".")[0])
            if sac_id not in place_ids:
                continue
            hut_id = place_ids[sac_id]
            raw = json.loads((RAW_DIR / f"{sac_id}.json").read_text())
            out = json.loads(out_file.read_text())
            r = out["result"] or {}
            # Quotes found word for word in the fetched pages are "verified"; PDFs aren't searchable here.
            corpus = f" {_words(' '.join(p['text'] for p in raw['pages']))} "

            def quoted(quote: str | None) -> bool | None:
                q = _words(quote)  # whole words: "Mitglieder 35" isn't "Mitglieder 350", an empty quote nothing
                found = bool(q) and f" {q} " in corpus
                return None if raw["pdfs"] and not found else found

            status = "unreachable" if not raw["pages"] else "ok" if r.get("about_this_hut") else "no_info"
            if status != "ok":
                r = {}
            conn.execute("DELETE FROM hut_season WHERE hut_id = ? AND source = 'scraped'", (hut_id,))
            conn.execute("DELETE FROM hut_price WHERE hut_id = ? AND source = 'scraped'", (hut_id,))
            conn.execute(
                "INSERT OR REPLACE INTO hut_web (hut_id, url, status, phone, email, booking_methods, booking_url, "
                "closed_otherwise, winter_room, winter_room_beds, beds, notes, fetched_at, extracted_at, model) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (hut_id, raw["website"], status, r.get("phone"), _email(r.get("email")), json.dumps(r.get("booking_methods") or []),
                 r.get("booking_url"), r.get("closed_otherwise"), r.get("winter_room"), r.get("winter_room_beds"),
                 r.get("beds"), _clean(r.get("notes")), raw["fetched_at"], out["extracted_at"], out["info"].get("model")))
            for s in r.get("seasons") or []:
                start, end = _month_day(s.get("start")), _month_day(s.get("end"))
                if not start or not end or s.get("kind") not in ("catered", "self_service", "closed") \
                        or (s["kind"] == "closed" and not s.get("year")):
                    counts["skipped_values"] += 1
                    continue
                conn.execute("INSERT INTO hut_season (hut_id, kind, start_md, end_md, year, approximate, note, "
                             "source, source_url, quote, verified) VALUES (?, ?, ?, ?, ?, ?, ?, 'scraped', ?, ?, ?)",
                             (hut_id, s["kind"], start, end, s.get("year"), bool(s.get("approximate")),
                              _clean(s.get("note")), s.get("source_url"), _clean(s.get("quote")),
                              quoted(s.get("quote"))))
                counts["seasons"] += 1
            for p in r.get("prices") or []:
                amounts = {k: _price(p.get(k)) for k in ("member", "non_member", "youth", "child")}
                if p.get("item") not in PRICE_ITEMS or not any(amounts.values()):
                    counts["skipped_values"] += 1
                    continue
                conn.execute("INSERT INTO hut_price (hut_id, item, label, includes_overnight, member, non_member, "
                             "youth, child, currency, year, source, source_url, quote, verified, updated_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scraped', ?, ?, ?, ?)",
                             (hut_id, p["item"], _clean(p.get("label")),
                              p["item"] == "half_board" and bool(p.get("includes_overnight")),
                              *amounts.values(), p.get("currency") or "CHF", p.get("year"), p.get("source_url"),
                              _clean(p.get("quote")), quoted(p.get("quote")), now))
                counts["prices"] += 1
            counts["huts"] += 1
            counts["ok"] += status == "ok"
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["fetch", "extract", "load"])
    parser.add_argument("--ids", help="comma-separated SAC ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--refetch", action="store_true", help="fetch / extract again even if saved")
    parser.add_argument("--model", default=gemini.DEFAULT_MODEL)
    args = parser.parse_args()
    conn = connect()
    ids = [int(i) for i in args.ids.split(",")] if args.ids else None
    huts = huts_to_scrape(conn, ids, args.limit)
    if args.stage == "fetch":
        fetch(huts, args.refetch)
    elif args.stage == "extract":
        extract(conn, huts, args.model, args.refetch)
    else:
        print(load(conn))


if __name__ == "__main__":
    main()

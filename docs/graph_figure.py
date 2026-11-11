"""Draw the hut-to-hut graph as an SVG for the README (light and dark mode).

    uv run python docs/graph_figure.py      # reads data/huthop.db, writes docs/hut-graph.svg

One straight line per hut pair that is connected within MAX_MIN in at least one direction (the faster direction
counts), coloured like the trail markings (yellow, red-white dashed, blue-white dashed) by the hardest trail
category along the route, using all category limits (up to unofficial OpenStreetMap paths, drawn dotted purple).
Positions are LV95, so distances are true to scale. Huts without any computed edge are drawn hollow.
"""

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data/huthop.db"
OUT = ROOT / "docs/hut-graph.svg"
MAX_MIN = 600  # the edge cutoff of build_edges, i.e. every stored hut pair
WIDTH = 960
PAD = 24
TOP = 64  # room for title and legend
CATEGORIES = {1: "Hiking (yellow)", 2: "Mountain (red-white)", 3: "Alpine (blue-white)", 4: "Unofficial (OSM)"}

# Per direction the edge of the most permissive category limit (as in app/api/edges.py), then per pair the
# faster direction.
PAIRS = """
WITH best AS (
  SELECT * FROM (
    SELECT e.*, ROW_NUMBER() OVER (
      PARTITION BY e.from_place, e.to_place
      ORDER BY e.source = 'manual' DESC, e.category_limit DESC) AS rn
    FROM edge e
    JOIN place a ON a.id = e.from_place AND a.kind = 'hut'
    JOIN place b ON b.id = e.to_place AND b.kind = 'hut'
    WHERE e.excluded = 0
  ) WHERE rn = 1
)
SELECT MIN(from_place, to_place) AS a, MAX(from_place, to_place) AS b, max_category, MIN(time_min) AS t
FROM best GROUP BY 1, 2 HAVING t <= :max
"""

STYLE = """
svg { --surface: #fcfcfb; --text: #0b0b0b; --muted: #52514e; --node: #0b0b0b;
      --c1: #eda100; --c2: #e34948; --c3: #2a78d6; --c4: #7a3e9d; }
@media (prefers-color-scheme: dark) {
  svg { --surface: #1a1a19; --text: #f0efec; --muted: #c3c2b7; --node: #f0efec;
        --c1: #c98500; --c2: #e34948; --c3: #3987e5; --c4: #b07cd0; }
}
text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; fill: var(--text); }
.sub { fill: var(--muted); font-size: 13px; }
.e { stroke-width: 1.2; stroke-linecap: round; stroke-opacity: 0.8; fill: none; }
.e1 { stroke: var(--c1); } .e2 { stroke: var(--c2); } .e3 { stroke: var(--c3); } .e4 { stroke: var(--c4); }
.e2, .e3 { stroke-dasharray: 5 2; stroke-linecap: butt; }  /* the white of the red-white and blue-white markings */
.e4 { stroke-dasharray: 1.5 2; stroke-linecap: butt; }  /* unofficial: no marking */
.hut { fill: var(--node); stroke: var(--surface); stroke-width: 1; }
.lone { fill: var(--surface); stroke: var(--node); stroke-width: 1.2; }
"""


def main() -> None:
    conn = sqlite3.connect(DB)
    huts = {i: (e, n) for i, e, n in conn.execute(
        "SELECT p.id, p.e_lv95, p.n_lv95 FROM place p JOIN hut h ON h.place_id = p.id")}
    pairs = conn.execute(PAIRS, {"max": MAX_MIN}).fetchall()
    connected = {i for (i,) in conn.execute(
        "SELECT from_place FROM edge UNION SELECT to_place FROM edge")}

    xs, ys = [e for e, _ in huts.values()], [n for _, n in huts.values()]
    scale = (WIDTH - 2 * PAD) / (max(xs) - min(xs))
    height = round(TOP + PAD + (max(ys) - min(ys)) * scale + PAD)

    def xy(i: int) -> tuple[float, float]:
        e, n = huts[i]
        return round(PAD + (e - min(xs)) * scale, 1), round(TOP + PAD + (max(ys) - n) * scale, 1)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" width="{WIDTH}" '
           f'height="{height}" role="img" aria-label="Map of Swiss huts and the walking connections between them">',
           f"<style>{STYLE}</style>",
           f'<rect width="{WIDTH}" height="{height}" fill="var(--surface)"/>',
           f'<text x="{PAD}" y="28" font-size="17" font-weight="600">HutHop route graph: {len(huts)} huts, '
           f"{len(pairs):,} hut pairs within {MAX_MIN // 60} h</text>"]
    # Legend, one row under the title
    x = PAD
    for cat, label in CATEGORIES.items():
        n = sum(1 for p in pairs if p[2] == cat)
        out.append(f'<line x1="{x}" y1="47" x2="{x + 18}" y2="47" class="e e{cat}" stroke-width="2.5" '
                   f'stroke-opacity="1"/><text x="{x + 24}" y="51" class="sub">{label} · {n:,}</text>')
        x += 24 + 6.6 * len(f"{label} · {n:,}") + 14
    out.append(f'<circle cx="{x + 5}" cy="47" r="3.5" class="lone"/>'
               f'<text x="{x + 14}" y="51" class="sub">No trail connection</text>')

    # Hardest category drawn first, so the easy routes stay visible on top
    for cat in sorted(CATEGORIES, reverse=True):
        segs = []
        for a, b, c, _ in pairs:
            if c == cat:
                (x1, y1), (x2, y2) = xy(a), xy(b)
                segs.append(f"M{x1} {y1}L{x2} {y2}")
        out.append(f'<path class="e e{cat}" d="{"".join(segs)}"/>')
    for i in huts:
        cx, cy = xy(i)
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{2.2 if i in connected else 3}" '
                   f'class="{"hut" if i in connected else "lone"}"/>')
    out.append("</svg>")
    OUT.write_text("\n".join(out) + "\n")
    print(f"{OUT.relative_to(ROOT)}: {len(huts)} huts, {len(pairs)} pairs, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()

// About dialog: how tours are found and scored ("best" weights from huthop.toml via /api/info), sources, impressum.
import { $, CATEGORY, esc } from "./util.js";

let infoPromise = null;

/** /api/info, fetched once. Resolves to null if it can't be loaded. */
export function loadInfo() {
  infoPromise ??= fetch("/api/info").then((r) => (r.ok ? r.json() : null)).catch(() => null);
  return infoPromise;
}

async function openAbout() {
  const info = await loadInfo();
  const metrics = (info?.metrics || []).filter((m) => m.weight > 0).sort((a, b) => b.weight - a.weight);
  const imp = info?.impressum || {};
  $("#about").innerHTML = `
    <div class="about-head">
      <h2>About HutHop</h2>
      <button class="about-close" data-close aria-label="Close">×</button>
    </div>
    <div class="about-body">
      <p>HutHop helps you plan multi-day hikes from hut to hut in Switzerland: browse the huts, then let it suggest
        tours that start and end at public transport, with one hut per night.</p>

      <h3>How tours are found</h3>
      <ul>
        <li>Walking connections follow swisstopo's signposted hiking trails. Times use the official Swiss
          hiking-time formula, without breaks.</li>
        <li>Each night's hut must be open in that month (SAC opening data), and with "Check free beds" on, have
          free beds for your group on hut-reservation.org (checked live, cached for 10 minutes).</li>
        <li>Each search returns up to 40 varied tours. Sorting reorders them instantly; "Best" is the default.</li>
      </ul>

      <h3>What makes a tour "best"</h3>
      <p>Every tour gets a score from 0 to 100 % for each criterion below. "Best" is their weighted average; the
        weights are fixed for everyone (set in the app's configuration).</p>
      <table class="about-table">
        <thead><tr><th>Criterion</th><th>Scores 100 % when…</th><th>Weight</th></tr></thead>
        <tbody>${metrics.map((m) => `<tr><td><b>${esc(m.label)}</b></td><td>${esc(m.description)}</td>
          <td class="num">${Math.round(m.share * 100)} %</td></tr>`).join("")}</tbody>
      </table>
      <p class="hint">Hover over a tour's score badge to see its individual scores.</p>

      <h3>Colours</h3>
      <div class="about-legend">
        <span><i class="dot catered"></i>Catered</span><span><i class="dot self_service"></i>Self-service</span>
        <span><i class="dot closed"></i>Closed</span><span><i class="dot unknown"></i>No opening data</span>
      </div>
      <div class="about-legend">${Object.values(CATEGORY).map((c) =>
        `<span><i class="line" style="background:${c.color}"></i>${c.label}: ${c.name.toLowerCase()}</span>`).join("")}</div>

      <h3>Data sources</h3>
      <ul>
        <li>Huts, opening months and access routes: <a href="https://www.sac-cas.ch" target="_blank" rel="noopener">SAC-CAS</a>
          (for private use only).</li>
        <li>Maps, hiking trails (swissTLM3D) and heights: <a href="https://www.swisstopo.admin.ch" target="_blank" rel="noopener">swisstopo</a>.</li>
        <li>Public transport stops: Federal Office of Transport (BAV), via data.geo.admin.ch.</li>
        <li>Free beds: <a href="https://www.hut-reservation.org" target="_blank" rel="noopener">hut-reservation.org</a>.
          HutHop only reads availability; book there or with the hut.</li>
      </ul>
      <p class="warn">HutHop is a planning aid. Trails, closures, snow and weather change: check conditions,
        the weather and official sources before you go, and confirm bookings with the hut.</p>

      <h3>Impressum</h3>
      ${imp.name || imp.contact ? `<p>${imp.name ? `${esc(imp.name)}<br>` : ""}${imp.contact ? esc(imp.contact) : ""}</p>` : ""}
      ${imp.note ? `<p>${esc(imp.note)}</p>` : ""}
      ${!imp.name && !imp.contact && !imp.note ? `<p class="hint">Not filled in yet ([impressum] in huthop.toml).</p>` : ""}
    </div>`;
  $("#about").showModal();
}

export function bindAbout() {
  $("#aboutBtn").addEventListener("click", openAbout);
  $("#about").addEventListener("click", (e) => {
    // Close on the × button or a click on the backdrop (outside the dialog box).
    if (e.target.closest("[data-close]") || e.target === $("#about")) $("#about").close();
  });
}

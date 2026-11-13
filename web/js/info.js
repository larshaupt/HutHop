// About dialog: how tours are found and scored ("best" weights from huthop.toml via /api/info), sources, impressum.
import { $, CATEGORY, esc } from "./util.js";
import { t } from "./i18n.js";

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
      <h2>${t("About HutHop")}</h2>
      <button class="about-close" data-close aria-label="${t("Close")}">×</button>
    </div>
    <div class="about-body">
      <p>${t("HutHop helps you plan multi-day hikes from hut to hut in Switzerland: browse the huts, then let it suggest tours that start and end at public transport, with one hut per night.")}</p>

      <h3>${t("How tours are found")}</h3>
      <ul>
        <li>${t("Walking connections follow swisstopo's signposted hiking trails. Times use the official Swiss hiking-time formula, without breaks.")}</li>
        <li>${t("Each night's hut must be open in that month (SAC opening data), and with “Check free beds” on, have free beds for your group on hut-reservation.org (checked live, cached for 10 minutes).")}</li>
        <li>${t("Each search returns up to 40 varied tours. Sorting reorders them instantly; “Best” is the default.")}</li>
      </ul>

      <h3>${t("What makes a tour “best”")}</h3>
      <p>${t("Every tour gets a score from 0 to 100 % for each criterion below. “Best” is their weighted average; the weights are fixed for everyone (set in the app's configuration).")}</p>
      <table class="about-table">
        <thead><tr><th>${t("Criterion")}</th><th>${t("Scores 100 % when…")}</th><th>${t("Weight")}</th></tr></thead>
        <tbody>${metrics.map((m) => `<tr><td><b>${esc(t(m.label))}</b></td><td>${esc(t(m.description))}</td>
          <td class="num">${Math.round(m.share * 100)} %</td></tr>`).join("")}</tbody>
      </table>
      <p class="hint">${t("Hover over a tour's score badge to see its individual scores.")}</p>

      <h3>${t("Colours")}</h3>
      <div class="about-legend">
        <span><i class="dot catered"></i>${t("Catered")}</span><span><i class="dot self_service"></i>${t("Self-service")}</span>
        <span><i class="dot closed"></i>${t("Closed")}</span><span><i class="dot unknown"></i>${t("No opening data")}</span>
      </div>
      <div class="about-legend">${Object.values(CATEGORY).map((c) =>
        `<span><i class="line" style="background:${c.color}"></i>${c.label}: ${c.name}</span>`).join("")}</div>

      <h3>${t("Data sources")}</h3>
      <ul>
        <li>${t("Huts, opening months and access routes:")} <a href="https://www.sac-cas.ch" target="_blank" rel="noopener">SAC-CAS</a>
          (${t("for private use only")}).</li>
        <li>${t("Maps, hiking trails (swissTLM3D) and heights:")} <a href="https://www.swisstopo.admin.ch" target="_blank" rel="noopener">swisstopo</a>.</li>
        <li>${t("Unofficial paths to huts away from the signposted trails:")} © <a href="https://www.openstreetmap.org/copyright"
          target="_blank" rel="noopener">OpenStreetMap</a> ${t("contributors (ODbL); their heights: Copernicus DEM via")}
          <a href="https://open-meteo.com" target="_blank" rel="noopener">Open-Meteo</a>.</li>
        <li>${t("Public transport stops: Federal Office of Transport (BAV), via data.geo.admin.ch.")}</li>
        <li>${t("Free beds:")} <a href="https://www.hut-reservation.org" target="_blank" rel="noopener">hut-reservation.org</a>.
          ${t("HutHop only reads availability; book there or with the hut.")}</li>
      </ul>
      <p class="warn">${t("HutHop is a planning aid. Trails, closures, snow and weather change: check conditions, the weather and official sources before you go, and confirm bookings with the hut.")}</p>

      <h3>Impressum</h3>
      ${imp.name || imp.contact ? `<p>${imp.name ? `${esc(imp.name)}<br>` : ""}${imp.contact ? esc(imp.contact) : ""}</p>` : ""}
      ${imp.note ? `<p>${esc(imp.note)}</p>` : ""}
      ${!imp.name && !imp.contact && !imp.note ? `<p class="hint">${t("Not filled in yet ([impressum] in huthop.toml).")}</p>` : ""}
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

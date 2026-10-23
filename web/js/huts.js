// Hut browser: filters, list, hut layers on the map and the hut detail view.
import { $, COLORS, MONTHS, STATUS_LABEL, esc, formatDuration, revealPanel } from "./util.js";
import { ALT_MAX, DEFAULTS, setState, state } from "./state.js";
import { map, mapPadding } from "./map.js";
import { clearConnections, connectionsSection, showConnections } from "./connections.js";
import { clearTour, returnToTour } from "./tours.js";
import { loadBeds } from "./beds.js";
import { formatHeight, heightUnit, heightValue } from "./settings.js";

const SERVICE_LABELS = {
  half_board: "Half board", plain_meals: "Simple meals", drinks: "Drinks",
  cooking_non_catered: "Guest kitchen (unstaffed)", cooking_catered: "Guest kitchen (staffed)",
  family_rooms: "Family rooms", separable_group_rooms: "Separate group rooms",
  payment_creditcard: "Card payment", payment_mobile: "Mobile payment",
  dogs_on_request: "Dogs on request", internet: "Internet",
};

let huts = [];
let visible = [];
const details = new Map();

export async function loadHuts() {
  const res = await fetch("/api/huts");
  if (!res.ok) throw new Error(`huts: ${res.status}`);
  huts = await res.json();
}

/** Reload the hut list after manual changes. */
export async function reloadHuts() {
  await loadHuts();
  update();
}

export function invalidateHut(id) {
  details.delete(id);
}

/** Opening status in the selected month (from the API's per-month list), or "any" without a month filter. */
const hutStatus = (hut) => (state.month ? hut.months[state.month - 1] : "any");

function filterHuts() {
  const q = state.q.trim().toLocaleLowerCase();
  const allowed = { any: ["catered", "self_service"], catered: ["catered"], self_service: ["self_service"] }[state.openAs];
  return huts.filter((h) =>
    (!state.month || allowed.includes(h.months[state.month - 1])) &&
    (!state.hb || h.half_board) &&
    (!state.kitchen || h.self_catering) &&
    (!state.bookable || h.bookable) &&
    (!state.minBeds || (h.beds || 0) >= state.minBeds) &&
    (h.altitude || 0) >= state.altMin &&
    (state.altMax >= ALT_MAX || (h.altitude || 0) <= state.altMax) &&
    (state.operator === "all" || h.operator === state.operator) &&
    (!q || h.name.toLocaleLowerCase().includes(q)),
  );
}

function sortHuts(list) {
  const byName = (a, b) => a.name.localeCompare(b.name, "de");
  const by = {
    name: byName,
    alt_desc: (a, b) => (b.altitude || 0) - (a.altitude || 0),
    alt_asc: (a, b) => (a.altitude || 0) - (b.altitude || 0),
    beds: (a, b) => (b.beds || 0) - (a.beds || 0),
  }[state.sort] || byName;
  return list.slice().sort(by);
}

// ---------- Filters ----------
export function renderControls() {
  $("#months").innerHTML = `<button data-m="0">Any</button>` +
    MONTHS.map((m, i) => `<button data-m="${i + 1}">${m}</button>`).join("");
  syncControls();
}

export function syncControls() {
  document.querySelectorAll("#months button").forEach((b) => b.setAttribute("aria-pressed", Number(b.dataset.m) === state.month));
  document.querySelectorAll("#openAs button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.v === state.openAs));
  document.querySelectorAll("#operator button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.v === state.operator));
  document.querySelectorAll("#base button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.v === state.base));
  document.querySelectorAll("[data-toggle]").forEach((b) => b.setAttribute("aria-pressed", state[b.dataset.toggle]));
  $("#trails").setAttribute("aria-pressed", state.trails);
  $("#openAs").style.opacity = state.month ? 1 : 0.5;
  $("#q").value = state.q;
  $("#sort").value = state.sort;
  $("#altMin").value = state.altMin;
  $("#altMax").value = state.altMax;
  $("#minBeds").value = state.minBeds;
  const hi = state.altMax >= ALT_MAX ? "max" : formatHeight(state.altMax);
  $("#altLabel").textContent = state.altMin || state.altMax < ALT_MAX ? `${heightValue(state.altMin)} – ${hi}` : "Any";
  $("#altFill").style.left = `${(state.altMin / ALT_MAX) * 100}%`;
  $("#altFill").style.right = `${100 - (state.altMax / ALT_MAX) * 100}%`;
  $("#bedsLabel").textContent = state.minBeds ? `${state.minBeds}+` : "Any";
  $("#legend").hidden = !state.month;

  const active = ["month", "hb", "kitchen", "bookable", "minBeds", "q"].filter((k) => state[k] !== DEFAULTS[k]).length +
    (state.altMin !== DEFAULTS.altMin || state.altMax !== DEFAULTS.altMax ? 1 : 0) +
    (state.operator !== "all" ? 1 : 0) + (state.month && state.openAs !== "any" ? 1 : 0);
  $("#activeCount").hidden = !active;
  $("#activeCount").textContent = active;
  $("#reset").hidden = !active;
}

export function bindHutEvents() {
  $("#months").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { setState({ month: Number(b.dataset.m) }); update(); }
  });
  for (const [id, key] of [["#openAs", "openAs"], ["#operator", "operator"]]) {
    $(id).addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) { setState({ [key]: b.dataset.v }); update(); }
    });
  }
  document.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => {
    setState({ [b.dataset.toggle]: !state[b.dataset.toggle] });
    update();
  }));
  $("#q").addEventListener("input", (e) => { setState({ q: e.target.value }); update(); });
  $("#sort").addEventListener("change", (e) => { setState({ sort: e.target.value }); update(); });
  $("#altMin").addEventListener("input", (e) => { setState({ altMin: Math.min(Number(e.target.value), state.altMax - 100) }); update(); });
  $("#altMax").addEventListener("input", (e) => { setState({ altMax: Math.max(Number(e.target.value), state.altMin + 100) }); update(); });
  $("#minBeds").addEventListener("input", (e) => { setState({ minBeds: Number(e.target.value) }); update(); });
  $("#reset").addEventListener("click", (e) => {
    e.preventDefault();
    setState({ month: 0, openAs: "any", hb: false, kitchen: false, bookable: false, altMin: 0, altMax: ALT_MAX,
      minBeds: 0, operator: "all", q: "" });
    update();
  });

  const list = $("#list");
  list.addEventListener("click", (e) => {
    const li = e.target.closest(".hut");
    if (li) selectHut(Number(li.dataset.id));
  });
  let hovered = null;
  const setHover = (id) => {
    if (hovered === id || !map.getSource("huts")) return;
    if (hovered) map.setFeatureState({ source: "huts", id: hovered }, { hover: false });
    hovered = id;
    if (id) map.setFeatureState({ source: "huts", id }, { hover: true });
  };
  list.addEventListener("mouseover", (e) => setHover(Number(e.target.closest(".hut")?.dataset.id) || null));
  list.addEventListener("mouseleave", () => setHover(null));
}

// ---------- List and map ----------
export function update() {
  syncControls();
  visible = sortHuts(filterHuts());
  $("#count").textContent = visible.length;
  $("#countLabel").textContent = visible.length === 1 ? "hut" : "huts";
  renderList();
  map?.getSource("huts")?.setData(hutsGeoJSON());
}

function renderList() {
  if (!visible.length) {
    $("#list").innerHTML = `<li class="empty">No huts match these filters.</li>`;
    return;
  }
  $("#list").innerHTML = visible.map((h) => {
    const status = hutStatus(h);
    const badge = status === "any" ? "" : `<span class="badge"><i class="dot ${status}"></i>${STATUS_LABEL[status]}</span>`;
    const img = h.photo
      ? `<img src="${esc(h.photo)}" alt="" loading="lazy" decoding="async" width="64" height="44">`
      : `<span class="noimg">▲</span>`;
    const meta = [h.altitude && formatHeight(h.altitude), h.beds && `${h.beds} beds`].filter(Boolean).join(" · ");
    return `<li class="hut" data-id="${h.id}">${img}<div><div class="hut-name">${esc(h.name)}</div>` +
      `<div class="hut-meta">${meta}</div></div>${badge}</li>`;
  }).join("");
}

function hutsGeoJSON() {
  return {
    type: "FeatureCollection",
    features: visible.map((h) => ({
      type: "Feature", id: h.id,
      geometry: { type: "Point", coordinates: [h.lon, h.lat] },
      properties: { id: h.id, name: h.name, altitude: h.altitude, beds: h.beds || 0, status: hutStatus(h) },
    })),
  };
}

export function addHutLayers() {
  map.addSource("huts", { type: "geojson", data: hutsGeoJSON() });
  const statusColor = ["match", ["get", "status"], ...Object.entries(COLORS).flat(), COLORS.any];
  const selected = ["case", ["boolean", ["feature-state", "selected"], false], 1, 0];
  map.addLayer({
    id: "huts-selected", type: "circle", source: "huts",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 10, 12, 15],
      "circle-color": "rgba(200,16,46,0.18)", "circle-stroke-color": COLORS.any, "circle-stroke-width": 2,
      "circle-opacity": selected, "circle-stroke-opacity": selected,
    },
  });
  map.addLayer({
    id: "huts", type: "circle", source: "huts",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 4, 9, 6, 13, 9],
      "circle-color": statusColor,
      "circle-stroke-color": ["match", ["get", "status"], "unknown", COLORS.closed, "#ffffff"],
      "circle-stroke-width": ["case", ["boolean", ["feature-state", "hover"], false], 3, 1.5],
    },
  });
  map.addLayer({
    id: "hut-labels", type: "symbol", source: "huts", minzoom: 10,
    layout: {
      "text-field": ["get", "name"], "text-font": ["Frutiger Neue Condensed Bold"],
      "text-size": ["interpolate", ["linear"], ["zoom"], 10, 11, 14, 13],
      // Try several positions around the dot before hiding a name; larger huts win when space is tight.
      "text-variable-anchor": ["top", "bottom", "right", "left"], "text-radial-offset": 0.75, "text-justify": "auto",
      "text-max-width": 9, "text-optional": true, "symbol-sort-key": ["-", 0, ["get", "beds"]],
    },
    paint: { "text-color": "#1d2521", "text-halo-color": "#ffffff", "text-halo-width": 1.6 },
  });
  if (state.hut) map.setFeatureState({ source: "huts", id: state.hut }, { selected: true });
}

let hutClickHook = null;
/** Handle clicks on hut dots elsewhere first; the hook returns true if it handled the click. */
export function setHutClickHook(fn) {
  hutClickHook = fn;
}

/** Hover popup and click on hut dots (bound once; MapLibre keeps layer handlers across style reloads). */
export function bindHutMap() {
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 12 });
  let hoverId = null;
  map.on("mousemove", "huts", (e) => {
    const f = e.features[0];
    map.getCanvas().style.cursor = "pointer";
    if (hoverId !== f.id) {
      if (hoverId) map.setFeatureState({ source: "huts", id: hoverId }, { hover: false });
      hoverId = f.id;
      map.setFeatureState({ source: "huts", id: hoverId }, { hover: true });
      document.querySelectorAll(".hut.hover").forEach((el) => el.classList.remove("hover"));
      document.querySelector(`.hut[data-id="${hoverId}"]`)?.classList.add("hover");
    }
    const status = f.properties.status;
    popup.setLngLat(f.geometry.coordinates)
      .setHTML(`${esc(f.properties.name)}<small>${f.properties.altitude ? formatHeight(f.properties.altitude) : ""}` +
        `${status !== "any" ? ` · ${STATUS_LABEL[status]}` : ""}</small>`)
      .addTo(map);
  });
  map.on("mouseleave", "huts", () => {
    map.getCanvas().style.cursor = "";
    if (hoverId) map.setFeatureState({ source: "huts", id: hoverId }, { hover: false });
    document.querySelectorAll(".hut.hover").forEach((el) => el.classList.remove("hover"));
    hoverId = null;
    popup.remove();
  });
  map.on("click", "huts", (e) => {
    const f = e.features[0];
    if (hutClickHook?.(f.id, f.properties.name)) return;  // e.g. the tour editor offers the edits the hut fits
    selectHut(f.id, { fly: false });
  });
}

// ---------- Detail view ----------
async function loadDetail(id) {
  if (!details.has(id)) {
    const res = await fetch(`/api/huts/${id}`);
    if (!res.ok) throw new Error(`hut ${id}: ${res.status}`);
    details.set(id, await res.json());
  }
  return details.get(id);
}

export async function selectHut(id, { fly = true } = {}) {
  if (map.getSource("huts")) {
    if (state.hut) map.setFeatureState({ source: "huts", id: state.hut }, { selected: false });
    map.setFeatureState({ source: "huts", id }, { selected: true });
  }
  setState({ hut: id });
  const hut = huts.find((h) => h.id === id);
  if (hut && fly) {
    map.flyTo({ center: [hut.lon, hut.lat], zoom: Math.max(map.getZoom(), 12), padding: mapPadding(), speed: 1.4 });
  }
  let d;
  try {
    d = await loadDetail(id);
  } catch (err) {
    console.error(err);
    return;
  }
  if (state.hut !== id) return; // another hut was selected meanwhile
  renderDetail(d);
  showConnections(id);
  $("#tourDetail").hidden = true;
  clearTour();
  $("#browse").hidden = true;
  $("#detail").hidden = false;
  $("#detail").scrollTop = 0;
  revealPanel();
}

export function closeDetail() {
  if (state.hut && map.getSource("huts")) map.setFeatureState({ source: "huts", id: state.hut }, { selected: false });
  setState({ hut: 0 });
  clearConnections();
  $("#detail").hidden = true;
  if (!(state.view === "tours" && state.tour && returnToTour())) $("#browse").hidden = false;
}

const BOOKING_LABEL = { hut_reservation_org: "hut-reservation.org", online_other: "online", email: "by email",
  phone: "by phone", not_needed: "not needed" };
const PRICE_LABEL = { overnight: "Night", half_board: "Half board", breakfast: "Breakfast", dinner: "Dinner",
  tax: "Tourist tax", winter_room: "Winter room", self_catering_fee: "Kitchen use" };
const UNVERIFIED = `<span class="unverified" title="Not found word for word on the website: check there">?</span>`;

function formatMonthDay(md) {
  const [m, d] = md.split("-").map(Number);
  return `${d} ${MONTHS[m - 1]}`;
}

function formatPrice(p) {
  const amount = (v) => (v == null ? "" : Number.isInteger(v) ? String(v) : v.toFixed(2));
  const main = p.member != null && p.non_member != null && p.member !== p.non_member
    ? `${amount(p.member)} members · ${amount(p.non_member)}` : amount(p.non_member ?? p.member);
  const reduced = [p.youth != null && `youth ${amount(p.youth)}`, p.child != null && `children ${amount(p.child)}`]
    .filter(Boolean).join(" · ");
  return `${p.currency} ${main}${reduced ? `<span class="route-meta"> · ${reduced}</span>` : ""}`;
}

// What the hut website says (read by app/ingest/scrape_huts.py); values may be outdated, so the source is linked.
function websiteSection(w) {
  if (!w || w.status !== "ok") return "";
  const seasons = w.seasons.map((s) => {
    const kind = { catered: "Catered", self_service: "Self-service", closed: "Closed" }[s.kind];
    const range = `${s.approximate ? "about " : ""}${formatMonthDay(s.start_md)} – ${formatMonthDay(s.end_md)}${s.year ? ` ${s.year}` : ""}`;
    return `<li><b>${kind}</b> ${range}${s.verified === 0 ? UNVERIFIED : ""}${s.note ? `<span class="route-meta"> · ${esc(s.note)}</span>` : ""}</li>`;
  });
  const prices = w.prices.map((p) => {
    const what = p.item === "half_board" && !p.includes_overnight ? "Half board (plus night)" : PRICE_LABEL[p.item];
    return `<tr><td>${what}<span class="route-meta"> ${esc(p.label || "")}</span></td>` +
      `<td>${formatPrice(p)}${p.verified === 0 ? UNVERIFIED : ""}</td></tr>`;
  });
  const booking = w.booking_methods.map((m) => BOOKING_LABEL[m]).filter(Boolean).join(", ");
  const priceYear = Math.max(...w.prices.map((p) => p.year || 0));
  let host = w.url;
  try { host = new URL(w.url).hostname.replace(/^www\./, ""); } catch { /* keep the URL */ }
  if (!seasons.length && !prices.length && !booking && !w.notes) return "";
  return `<div class="section website-info"><h3>From the hut website</h3>
    ${booking ? `<p>Booking: ${booking}${w.booking_url ? ` · <a href="${esc(w.booking_url)}" target="_blank" rel="noopener">booking page</a>` : ""}</p>` : ""}
    ${w.notes ? `<p>${esc(w.notes)}</p>` : ""}
    ${seasons.length ? `<ul class="web-seasons">${seasons.join("")}</ul>` : ""}
    ${prices.length ? `<table class="prices"><caption>Prices per person and night${priceYear ? ` (${priceYear})` : ""}</caption>${prices.join("")}</table>` : ""}
    <p class="hint">Read from <a href="${esc(w.url)}" target="_blank" rel="noopener">${esc(host)}</a> by AI on
      ${new Date(w.checked_at).toLocaleDateString("de-CH")}; may be outdated or wrong. Check there before you go.</p>
  </div>`;
}

function renderDetail(d) {
  const photo = d.photos[0];
  const currentMonth = new Date().getMonth();
  const subtitle = [d.operator === "sac" ? "SAC hut" : "Hut", d.owner && `owned by ${esc(d.owner)}`].filter(Boolean).join(" · ");
  const services = Object.entries(SERVICE_LABELS).filter(([k]) => d.services[k]).map(([, label]) => `<span>${label}</span>`);
  const routes = d.routes.map((r) => {
    const grade = (r.main_difficulty || "").trim();
    const time = formatDuration(r.ascent_time_min, r.ascent_time_max);
    const meta = [time, r.ascent_altitude && `↑ ${formatHeight(r.ascent_altitude)}`, r.descent_altitude && `↓ ${formatHeight(r.descent_altitude)}`,
      r.normal_route && "normal route"].filter(Boolean).join(" · ");
    return `<li><span class="grade g${grade.charAt(1) || 0}">${esc(grade || "–")}</span>` +
      `<span class="route-title">${esc(r.title)}</span><span class="route-meta">${meta}</span></li>`;
  });
  const [e, n] = d.lv95.map(Math.round);
  const geoAdmin = `https://map.geo.admin.ch/#/map?lang=en&center=${e},${n}&z=9&bgLayer=ch.swisstopo.pixelkarte-farbe` +
    "&layers=ch.swisstopo.swisstlm3d-wanderwege&crosshair=marker";
  const long = (d.description || "").length > 320;

  $("#detail").innerHTML = `
    <div class="hero">
      ${photo ? `<img src="${esc(photo.large || photo.medium)}" alt="${esc(photo.caption || d.name)}">` : ""}
      <button class="back" id="back"><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>${state.view === "tours" && state.tour ? "Tour" : "All huts"}</button>
      ${photo ? `<span class="credit">© ${esc(photo.copyright)}</span>` : ""}
    </div>
    <div class="detail-body">
      <h2>${esc(d.name)}</h2>
      <div class="subtitle">${subtitle}</div>
      <div class="facts">
        <div class="fact"><b>${d.altitude ? heightValue(d.altitude) : "–"}</b><span>${heightUnit()} altitude</span></div>
        <div class="fact"><b>${d.beds ?? "–"}</b><span>beds</span></div>
        <div class="fact"><b>${d.shelter_beds ?? "–"}</b><span>winter room</span></div>
      </div>
      <div class="actions">
        ${d.booking_url ? `<a class="btn primary" href="${esc(d.booking_url)}" target="_blank" rel="noopener">Check availability</a>` : ""}
        ${d.website ? `<a class="btn" href="${esc(d.website)}" target="_blank" rel="noopener">Website</a>` : ""}
        ${d.sac_url ? `<a class="btn" href="${esc(d.sac_url)}" target="_blank" rel="noopener">SAC page</a>` : ""}
        ${d.email ? `<a class="btn" href="mailto:${esc(d.email)}">Email</a>` : ""}
        ${d.phone ? `<a class="btn" href="tel:${esc(d.phone.replace(/[^\d+]/g, ""))}">${esc(d.phone)}</a>` : ""}
        <a class="btn" href="${geoAdmin}" target="_blank" rel="noopener">swisstopo map</a>
        <button class="btn" data-plan-tour="${d.id}">Plan a tour from here</button>
      </div>
      <div class="section">
        <h3>Season</h3>
        <div class="season">${d.months.map((s, i) =>
          `<div class="${i === currentMonth ? "now" : ""}" title="${MONTHS[i]}: ${STATUS_LABEL[s]}${d.partial_months[i] ? " (part of the month)" : ""}">` +
          `<i class="${s}${d.partial_months[i] ? " partial" : ""}"></i>${MONTHS[i].charAt(0)}</div>`).join("")}</div>
        <div class="season-legend">
          <span><i class="dot catered"></i>Catered</span><span><i class="dot self_service"></i>Self-service only</span>
          <span><i class="dot closed"></i>Closed</span>${d.months[0] === "unknown" ? `<span><i class="dot unknown"></i>No data</span>` : ""}
          <span>Half-filled: part of the month</span>
        </div>
      </div>
      ${websiteSection(d.web)}
      ${d.booking_url ? `<div class="section"><h3>Free beds</h3><div id="bedCal"><p class="hint">Checking free beds…</p></div></div>` : ""}
      ${connectionsSection(d)}
      ${d.description ? `<div class="section"><h3>About</h3><p class="desc ${long ? "clamped" : ""}" id="desc">${esc(d.description)}</p>
        ${long ? `<button class="link" id="more">Show more</button>` : ""}</div>` : ""}
      ${routes.length ? `<div class="section"><h3>Access routes</h3><ul class="routes">${routes.join("")}</ul></div>` : ""}
      ${services.length ? `<div class="section"><h3>Services</h3><div class="services">${services.join("")}</div></div>` : ""}
      <p class="source">${d.source === "manual" ? "Hut added by hand" : `Hut data © <a href="https://www.sac-cas.ch" target="_blank" rel="noopener">SAC-CAS</a>,
        synced ${d.synced_at ? new Date(d.synced_at).toLocaleDateString("de-CH") : "–"}`}. Check conditions and official sources before you go.
        <a href="#" data-edit-hut="${d.id}">Edit hut data</a></p>
    </div>`;
  $("#back").addEventListener("click", closeDetail);
  if (d.booking_url) loadBeds(d.id);
  $("#more")?.addEventListener("click", (ev) => {
    $("#desc").classList.remove("clamped");
    ev.target.remove();
  });
}

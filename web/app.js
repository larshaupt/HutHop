"use strict";

// ---------- Constants ----------
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const ALT_MAX = 4600;
const CH_BOUNDS = [[5.9, 45.8], [10.5, 47.85]];
const COLORS = { catered: "#2e8b57", self_service: "#2f6fb3", closed: "#9aa3a0", unknown: "#ffffff", any: "#c8102e" };
const STATUS_LABEL = { catered: "Catered", self_service: "Self-service", closed: "Closed", unknown: "No data" };
const SERVICE_LABELS = {
  half_board: "Half board", plain_meals: "Simple meals", drinks: "Drinks",
  cooking_non_catered: "Guest kitchen (unstaffed)", cooking_catered: "Guest kitchen (staffed)",
  family_rooms: "Family rooms", separable_group_rooms: "Separate group rooms",
  payment_creditcard: "Card payment", payment_mobile: "Mobile payment",
  dogs_on_request: "Dogs on request", internet: "Internet",
};
const ATTRIBUTION = '<a href="https://www.swisstopo.admin.ch" target="_blank" rel="noopener">© swisstopo</a> · ' +
  'Hut data <a href="https://www.sac-cas.ch" target="_blank" rel="noopener">© SAC-CAS</a>';
const STYLES = {
  light: "https://vectortiles.geo.admin.ch/styles/ch.swisstopo.lightbasemap.vt/style.json",
  topo: {
    version: 8,
    glyphs: "https://vectortiles.geo.admin.ch/fonts/{fontstack}/{range}.pbf",
    sources: {
      topo: {
        type: "raster", tileSize: 256, maxzoom: 18,
        tiles: ["https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg"],
      },
    },
    layers: [{ id: "topo", type: "raster", source: "topo" }],
  },
};
const CATEGORY = {
  1: { label: "Yellow", name: "Hiking trail", color: "#e0a800" },
  2: { label: "Red-white", name: "Mountain trail", color: "#d52b1e" },
  3: { label: "Blue-white", name: "Alpine trail", color: "#1f5fbf" },
};
const MODE_LABELS = { cableway: "Cable car", chairlift: "Chairlift", funicular: "Funicular", rack_railway: "Rack railway",
  train: "Train", boat: "Boat", tram: "Tram", metro: "Metro", elevator: "Lift", bus: "Bus" };
const TRAILS_TILES = "https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swisstlm3d-wanderwege/default/current/3857/{z}/{x}/{y}.png";

// ---------- State (mirrored in the URL hash, so views can be shared and bookmarked) ----------
const DEFAULTS = {
  month: 0, openAs: "any", hb: false, kitchen: false, bookable: false,
  altMin: 0, altMax: ALT_MAX, minBeds: 0, operator: "all", q: "", sort: "name",
  hut: 0, base: "light", trails: false, limit: 2,
  // tour planner
  view: "huts", tDate: "", tNights: 2, tMin: 3, tMax: 6, tCat: "any", tStart: 0, tStartName: "", tLoop: false,
  tArea: true, tUnknown: true, tBox: "", tour: 0, tPeople: 2, tAvail: true,
};
const HASH_KEYS = { month: "m", openAs: "open", hb: "hb", kitchen: "kitchen", bookable: "book", altMin: "amin",
  altMax: "amax", minBeds: "beds", operator: "type", q: "q", sort: "sort", hut: "hut", base: "map", trails: "trails", limit: "tl",
  view: "v", tDate: "td", tNights: "tn", tMin: "tmin", tMax: "tmax", tCat: "tc", tStart: "ts", tStartName: "tsn",
  tLoop: "loop", tArea: "area", tUnknown: "unk", tBox: "tb", tour: "tour", tPeople: "ppl", tAvail: "avail" };
let state = readHash();
let huts = [];
let visible = [];
const details = new Map();
const connections = new Map(); // "hutId:limit" → API response
let map;

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const s = { ...DEFAULTS };
  for (const [key, short] of Object.entries(HASH_KEYS)) {
    if (!params.has(short)) continue;
    const raw = params.get(short);
    const def = DEFAULTS[key];
    s[key] = typeof def === "boolean" ? raw === "1" : typeof def === "number" ? Number(raw) || 0 : raw;
  }
  return s;
}

function writeHash() {
  const params = new URLSearchParams();
  for (const [key, short] of Object.entries(HASH_KEYS)) {
    const v = state[key];
    if (v === DEFAULTS[key]) continue;
    params.set(short, typeof v === "boolean" ? (v ? "1" : "0") : v);
  }
  const hash = params.toString();
  history.replaceState(null, "", hash ? `#${hash}` : location.pathname);
}

function setState(patch) {
  state = { ...state, ...patch };
  writeHash();
}

// ---------- Helpers ----------
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/** Status of a hut in a month (1–12) from SAC codes: 0 = whole month, 1 = partly, 2 = no; all 2 = no data. */
function monthStatus(hut, month) {
  if (!/[01]/.test(hut.catering + hut.self_service)) return "unknown";
  if ("01".includes(hut.catering[month - 1])) return "catered";
  if ("01".includes(hut.self_service[month - 1])) return "self_service";
  return "closed";
}

function hutStatus(hut) {
  return state.month ? monthStatus(hut, state.month) : "any";
}

function formatDuration(min, max) {
  const fmt = (m) => {
    const h = Math.floor(m / 60), r = m % 60;
    return r ? `${h}:${String(r).padStart(2, "0")}` : `${h}`;
  };
  if (!min && !max) return null;
  if (min && max && min !== max) return `${fmt(min)}–${fmt(max)} h`;
  return `${fmt(max || min)} h`;
}

function formatMinutes(min) {
  if (min == null) return "–";
  const h = Math.floor(min / 60), m = min % 60;
  return `${h}:${String(m).padStart(2, "0")} h`;
}

function decodePolyline(text) {
  const points = [];
  let index = 0, lat = 0, lon = 0;
  while (index < text.length) {
    for (const axis of [0, 1]) {
      let shift = 0, result = 0, b;
      do {
        b = text.charCodeAt(index++) - 63;
        result |= (b & 0x1f) << shift;
        shift += 5;
      } while (b >= 0x20);
      const delta = result & 1 ? ~(result >> 1) : result >> 1;
      if (axis === 0) lat += delta; else lon += delta;
    }
    points.push([lon / 1e5, lat / 1e5]);
  }
  return points;
}

function mainMode(mode) {
  const modes = (mode || "").split(",");
  return Object.keys(MODE_LABELS).find((m) => modes.includes(m)) || "bus";
}

function filterHuts() {
  const q = state.q.trim().toLocaleLowerCase();
  const allowed = { any: ["catered", "self_service"], catered: ["catered"], self_service: ["self_service"] }[state.openAs];
  return huts.filter((h) =>
    (!state.month || allowed.includes(monthStatus(h, state.month))) &&
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
  const by = {
    name: (a, b) => a.name.localeCompare(b.name, "de"),
    alt_desc: (a, b) => (b.altitude || 0) - (a.altitude || 0),
    alt_asc: (a, b) => (a.altitude || 0) - (b.altitude || 0),
    beds: (a, b) => (b.beds || 0) - (a.beds || 0),
  }[state.sort] || ((a, b) => a.name.localeCompare(b.name, "de"));
  return list.slice().sort(by);
}

// ---------- Rendering: filters ----------
function renderControls() {
  $("#months").innerHTML = `<button data-m="0">Any</button>` +
    MONTHS.map((m, i) => `<button data-m="${i + 1}">${m}</button>`).join("");
  syncControls();
}

function syncControls() {
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
  const hi = state.altMax >= ALT_MAX ? "max" : `${state.altMax.toLocaleString("de-CH")} m`;
  $("#altLabel").textContent = state.altMin || state.altMax < ALT_MAX ? `${state.altMin.toLocaleString("de-CH")} – ${hi}` : "Any";
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

// ---------- Rendering: list and map ----------
function update() {
  syncControls();
  visible = sortHuts(filterHuts());
  $("#count").textContent = visible.length;
  $("#countLabel").textContent = visible.length === 1 ? "hut" : "huts";
  renderList();
  updateMapData();
}

function renderList() {
  if (!visible.length) {
    $("#list").innerHTML = `<li class="empty">No huts match these filters.</li>`;
    return;
  }
  $("#list").innerHTML = visible.map((h) => {
    const status = hutStatus(h);
    const badge = status === "any" ? "" :
      `<span class="badge"><i class="dot ${status}"></i>${STATUS_LABEL[status]}</span>`;
    const img = h.photo
      ? `<img src="${esc(h.photo)}" alt="" loading="lazy" decoding="async" width="64" height="44">`
      : `<span class="noimg">▲</span>`;
    const meta = [h.altitude && `${h.altitude.toLocaleString("de-CH")} m`, h.beds && `${h.beds} beds`].filter(Boolean).join(" · ");
    return `<li class="hut" data-id="${h.id}">${img}<div><div class="hut-name">${esc(h.name)}</div>` +
      `<div class="hut-meta">${meta}</div></div>${badge}</li>`;
  }).join("");
}

function hutsGeoJSON() {
  return {
    type: "FeatureCollection",
    features: visible.map((h) => ({
      type: "Feature",
      id: h.id,
      geometry: { type: "Point", coordinates: [h.lon, h.lat] },
      properties: { id: h.id, name: h.name, altitude: h.altitude, beds: h.beds || 0, status: hutStatus(h) },
    })),
  };
}

function updateMapData() {
  const source = map?.getSource("huts");
  if (source) source.setData(hutsGeoJSON());
}

function addHutLayers() {
  if (!map.getSource("trails")) {
    map.addSource("trails", { type: "raster", tiles: [TRAILS_TILES], tileSize: 256, maxzoom: 18 });
  }
  map.addLayer({ id: "trails", type: "raster", source: "trails",
    paint: { "raster-opacity": ["interpolate", ["linear"], ["zoom"], 7, 0.45, 10, 0.9] },
    layout: { visibility: state.trails ? "visible" : "none" } });
  const empty = { type: "FeatureCollection", features: [] };
  map.addSource("edges", { type: "geojson", data: empty });
  map.addSource("edge-stops", { type: "geojson", data: empty });
  map.addLayer({
    id: "edges-casing", type: "line", source: "edges", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#ffffff", "line-opacity": 0.85,
      "line-width": ["case", ["boolean", ["feature-state", "hover"], false], 9, 5] },
  });
  // MapLibre 4 has no data-driven dash patterns, so excluded edges get their own dashed layer.
  const edgePaint = {
    "line-color": ["match", ["get", "category"], 1, CATEGORY[1].color, 2, CATEGORY[2].color, CATEGORY[3].color],
    "line-width": ["case", ["boolean", ["feature-state", "hover"], false], 5, 2.5],
  };
  map.addLayer({
    id: "edges", type: "line", source: "edges", filter: ["!", ["get", "excluded"]],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { ...edgePaint, "line-opacity": ["case", ["boolean", ["feature-state", "hover"], false], 1, 0.8] },
  });
  map.addLayer({
    id: "edges-excluded", type: "line", source: "edges", filter: ["get", "excluded"],
    paint: { ...edgePaint, "line-opacity": 0.45, "line-dasharray": [1.5, 2] },
  });
  map.addLayer({
    id: "edge-stops", type: "circle", source: "edge-stops",
    paint: { "circle-radius": 4.5, "circle-color": "#1d2521", "circle-stroke-color": "#fff", "circle-stroke-width": 1.5 },
  });
  map.addLayer({
    id: "edge-stop-labels", type: "symbol", source: "edge-stops", minzoom: 10,
    layout: { "text-field": ["get", "name"], "text-font": ["Frutiger Neue Condensed Regular"], "text-size": 11,
      "text-variable-anchor": ["top", "bottom", "right", "left"], "text-radial-offset": 0.6, "text-optional": true },
    paint: { "text-color": "#3b4540", "text-halo-color": "#fff", "text-halo-width": 1.4 },
  });
  map.addSource("huts", { type: "geojson", data: hutsGeoJSON() });
  const statusColor = ["match", ["get", "status"], ...Object.entries(COLORS).flat(), COLORS.any];
  map.addLayer({
    id: "huts-selected", type: "circle", source: "huts",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 10, 12, 15],
      "circle-color": "rgba(200,16,46,0.18)",
      "circle-stroke-color": COLORS.any, "circle-stroke-width": 2,
      "circle-opacity": ["case", ["boolean", ["feature-state", "selected"], false], 1, 0],
      "circle-stroke-opacity": ["case", ["boolean", ["feature-state", "selected"], false], 1, 0],
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
  addTourLayers();
  if (state.hut) {
    map.setFeatureState({ source: "huts", id: state.hut }, { selected: true });
    const data = connections.get(`${state.hut}:${state.limit}`);
    if (data) drawConnections(data);
  } else if (state.tour && tourResults) {
    drawTour(tourResults.tours[state.tour - 1], { fit: false });
  }
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

async function selectHut(id, { fly = true } = {}) {
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
  if (isMobile() && $("#panel").dataset.sheet === "peek") setSheet("half");
}

function closeDetail() {
  if (state.hut && map.getSource("huts")) map.setFeatureState({ source: "huts", id: state.hut }, { selected: false });
  setState({ hut: 0 });
  clearConnections();
  $("#detail").hidden = true;
  if (!(state.view === "tours" && state.tour && returnToTour())) $("#browse").hidden = false;
}

function renderDetail(d) {
  const photo = d.photos[0];
  const currentMonth = new Date().getMonth();
  const subtitle = [d.operator === "sac" ? "SAC hut" : "Hut", d.owner && `owned by ${esc(d.owner)}`].filter(Boolean).join(" · ");
  const services = Object.entries(SERVICE_LABELS).filter(([k]) => d.services[k]).map(([, label]) => `<span>${label}</span>`);
  const routes = d.routes.map((r) => {
    const grade = (r.main_difficulty || "").trim();
    const time = formatDuration(r.ascent_time_min, r.ascent_time_max);
    const meta = [time, r.ascent_altitude && `↑ ${r.ascent_altitude} m`, r.descent_altitude && `↓ ${r.descent_altitude} m`,
      r.normal_route && "normal route"].filter(Boolean).join(" · ");
    return `<li><span class="grade g${grade.charAt(1) || 0}">${esc(grade || "–")}</span>` +
      `<span class="route-title">${esc(r.title)}</span><span class="route-meta">${meta}</span></li>`;
  });
  const [e, n] = d.lv95.map(Math.round);
  const geoAdmin = `https://map.geo.admin.ch/#/map?lang=en&center=${e},${n}&z=9&bgLayer=ch.swisstopo.pixelkarte-farbe&layers=ch.swisstopo.swisstlm3d-wanderwege&crosshair=marker`;
  const long = (d.description || "").length > 320;

  $("#detail").innerHTML = `
    <div class="hero">
      ${photo ? `<img src="${esc(photo.large || photo.medium)}" alt="${esc(photo.caption || d.name)}">` : ""}
      <button class="back" id="back"><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>All huts</button>
      ${photo ? `<span class="credit">© ${esc(photo.copyright)}</span>` : ""}
    </div>
    <div class="detail-body">
      <h2>${esc(d.name)}</h2>
      <div class="subtitle">${subtitle}</div>
      <div class="facts">
        <div class="fact"><b>${d.altitude ? d.altitude.toLocaleString("de-CH") : "–"}</b><span>m altitude</span></div>
        <div class="fact"><b>${d.beds ?? "–"}</b><span>beds</span></div>
        <div class="fact"><b>${d.shelter_beds ?? "–"}</b><span>winter room</span></div>
      </div>
      <div class="actions">
        ${d.booking_url ? `<a class="btn primary" href="${esc(d.booking_url)}" target="_blank" rel="noopener">Check availability</a>` : ""}
        ${d.website ? `<a class="btn" href="${esc(d.website)}" target="_blank" rel="noopener">Website</a>` : ""}
        <a class="btn" href="${geoAdmin}" target="_blank" rel="noopener">swisstopo map</a>
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
      <div class="section" id="connections">
        <h3>Connections</h3>
        <div class="seg trail-seg" id="limit" role="group" aria-label="Hardest trail allowed">
          ${[1, 2, 3].map((c) => `<button data-v="${c}" aria-pressed="${c === state.limit}">` +
            `<i class="trail-swatch c${c}"></i>${c === 1 ? "Yellow" : c === 2 ? "Red-white" : "Blue-white"}</button>`).join("")}
        </div>
        <p class="hint">Hardest trail allowed. Walking times use the Swiss hiking-time formula, without breaks.</p>
        ${d.snap_m > 500 ? `<p class="warn">This hut is ${(d.snap_m / 1000).toFixed(1)} km from the nearest signposted
          hiking trail. It is reached by alpine routes or across glaciers, so no walking connections are computed.</p>`
          : d.snap_m > 200 ? `<p class="warn">This hut is ${Math.round(d.snap_m)} m from the nearest signposted trail,
          so connection times may be inaccurate.</p>` : ""}
        <div id="connList"><p class="hint">Loading…</p></div>
      </div>
      ${d.description ? `<div class="section"><h3>About</h3><p class="desc ${long ? "clamped" : ""}" id="desc">${esc(d.description)}</p>
        ${long ? `<button class="link" id="more">Show more</button>` : ""}</div>` : ""}
      ${routes.length ? `<div class="section"><h3>Access routes</h3><ul class="routes">${routes.join("")}</ul></div>` : ""}
      ${services.length ? `<div class="section"><h3>Services</h3><div class="services">${services.join("")}</div></div>` : ""}
      <p class="source">Hut data © <a href="https://www.sac-cas.ch" target="_blank" rel="noopener">SAC-CAS</a>,
        synced ${d.synced_at ? new Date(d.synced_at).toLocaleDateString("de-CH") : "–"}. Check conditions and official sources before you go.</p>
    </div>`;
  $("#back").addEventListener("click", closeDetail);
  $("#limit").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    setState({ limit: Number(b.dataset.v) });
    document.querySelectorAll("#limit button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    showConnections(d.id);
  });
  $("#more")?.addEventListener("click", (ev) => {
    $("#desc").classList.remove("clamped");
    ev.target.remove();
  });
}

// ---------- Connections (edges of the place graph) ----------
async function loadConnections(hutId, limit) {
  const key = `${hutId}:${limit}`;
  if (!connections.has(key)) {
    const res = await fetch(`/api/huts/${hutId}/edges?limit=${limit}`);
    if (!res.ok) throw new Error(`edges ${hutId}: ${res.status}`);
    connections.set(key, await res.json());
  }
  return connections.get(key);
}

async function showConnections(hutId) {
  const limit = state.limit;
  let data;
  try {
    data = await loadConnections(hutId, limit);
  } catch (err) {
    console.error(err);
    if ($("#connList")) $("#connList").innerHTML = `<p class="hint">Could not load connections.</p>`;
    return;
  }
  if (state.hut !== hutId || state.limit !== limit || !$("#connList")) return;
  renderConnections(data);
  drawConnections(data);
}

function edgeRow(e, label, kind) {
  const cat = CATEGORY[e.max_category];
  const meta = [`${(e.distance_m / 1000).toFixed(1)} km`, `↑ ${e.ascent_m} m`, `↓ ${e.descent_m} m`];
  const sac = e.sac_time_min ? `<span class="sac" title="Time given by the SAC for this route">SAC ${formatMinutes(e.sac_time_min)}` +
    `${e.sac_grade ? ` · ${esc(e.sac_grade)}` : ""}</span>` : "";
  return `<li class="conn${e.excluded ? " excluded" : ""}" data-edge="${e.id}" data-place="${e.place.id}" data-kind="${kind}">
    <i class="cat-bar" style="background:${cat.color}" title="${cat.name}"></i>
    <div class="conn-main"><div class="conn-name">${label}</div>
      <div class="conn-meta">${meta.join(" · ")} ${sac}</div></div>
    <div class="conn-time">${formatMinutes(e.time_min)}</div>
    <button class="conn-x" data-exclude="${e.id}" title="${e.excluded ? "Include this connection again" : "Exclude this connection (bad route)"}"
      aria-label="${e.excluded ? "Include" : "Exclude"}">${e.excluded ? "↺" : "×"}</button>
  </li>`;
}

const STOPS_SHOWN = 8;

function renderConnections(data, showAllStops = false) {
  const hutRows = data.huts.map((e) => edgeRow(e, esc(e.place.name), "hut"));
  const stopRows = [];
  for (const s of showAllStops ? data.stops : data.stops.slice(0, STOPS_SHOWN)) {
    const mode = MODE_LABELS[mainMode(s.place.mode)];
    for (const [dir, arrow] of [["to_hut", "from"], ["from_hut", "to"]]) {
      const e = s[dir];
      if (e) stopRows.push(edgeRow({ ...e, place: s.place }, `<span class="mode">${mode}</span> ${arrow} ${esc(s.place.name)}`, "stop"));
    }
  }
  $("#connList").innerHTML = `
    <h4>Huts within a day <span>${data.huts.length}</span></h4>
    ${hutRows.length ? `<ul class="conns">${hutRows.join("")}</ul>` : `<p class="hint">No huts within 10 hours on these trails.</p>`}
    <h4>Public transport <span>${data.stops.length}</span></h4>
    ${stopRows.length ? `<ul class="conns">${stopRows.join("")}</ul>` : `<p class="hint">No stops within 10 hours on these trails.</p>`}
    ${!showAllStops && data.stops.length > STOPS_SHOWN ? `<button class="link" id="allStops">Show all ${data.stops.length} stops</button>` : ""}`;
  $("#allStops")?.addEventListener("click", () => renderConnections(data, true));
}

function drawConnections(data) {
  if (!map.getSource("edges")) return;
  const edges = [...data.huts, ...data.stops.flatMap((s) => [s.to_hut, s.from_hut].filter(Boolean))];
  map.getSource("edges").setData({
    type: "FeatureCollection",
    features: edges.map((e) => ({
      type: "Feature", id: e.id,
      geometry: { type: "LineString", coordinates: decodePolyline(e.geometry) },
      properties: { category: e.max_category, excluded: e.excluded },
    })),
  });
  map.getSource("edge-stops").setData({
    type: "FeatureCollection",
    features: data.stops.map((s) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [s.place.lon, s.place.lat] },
      properties: { name: s.place.name },
    })),
  });
}

function clearConnections() {
  const empty = { type: "FeatureCollection", features: [] };
  map.getSource("edges")?.setData(empty);
  map.getSource("edge-stops")?.setData(empty);
}

async function toggleExcluded(edgeId) {
  const data = connections.get(`${state.hut}:${state.limit}`);
  const all = [...data.huts, ...data.stops.flatMap((s) => [s.to_hut, s.from_hut].filter(Boolean))];
  const edge = all.find((e) => e.id === edgeId);
  const res = await fetch(`/api/edges/${edgeId}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ excluded: !edge.excluded }),
  });
  if (!res.ok) return;
  const { excluded } = await res.json();
  for (const [key, cached] of connections) {  // the same edge can appear under several limits
    for (const e of [...cached.huts, ...cached.stops.flatMap((s) => [s.to_hut, s.from_hut].filter(Boolean))]) {
      if (e.id === edgeId) e.excluded = excluded;
    }
  }
  renderConnections(data);
  drawConnections(data);
}

// ---------- Tabs ----------
function setView(view) {
  setState({ view });
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === view));
  $("#hutsTab").hidden = view !== "huts";
  $("#toursTab").hidden = view !== "tours";
  if (view === "huts") {
    if (state.tour) setState({ tour: 0 });
    clearTour();
  } else if (tourResults && !state.tour) {
    clearTour();
  }
}

// ---------- Layout helpers ----------
const isMobile = () => matchMedia("(max-width: 720px)").matches;

function mapPadding() {
  if (isMobile()) return { top: 60, bottom: window.innerHeight * 0.55, left: 20, right: 20 };
  return { top: 60, bottom: 40, left: $("#panel").offsetWidth + 40, right: 40 };
}

function setSheet(mode) {
  $("#panel").dataset.sheet = mode;
}

// ---------- Events ----------
function bindEvents() {
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) setView(b.dataset.view);
  });
  bindTourForm();

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
  $("#altMin").addEventListener("input", (e) => {
    setState({ altMin: Math.min(Number(e.target.value), state.altMax - 100) });
    update();
  });
  $("#altMax").addEventListener("input", (e) => {
    setState({ altMax: Math.max(Number(e.target.value), state.altMin + 100) });
    update();
  });
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

  let hoveredEdge = null;
  const setEdgeHover = (id) => {
    if (hoveredEdge === id || !map.getSource("edges")) return;
    if (hoveredEdge) map.setFeatureState({ source: "edges", id: hoveredEdge }, { hover: false });
    hoveredEdge = id;
    if (id) map.setFeatureState({ source: "edges", id }, { hover: true });
  };
  $("#detail").addEventListener("mouseover", (e) => setEdgeHover(Number(e.target.closest(".conn")?.dataset.edge) || null));
  $("#detail").addEventListener("mouseleave", () => setEdgeHover(null));
  $("#detail").addEventListener("click", (e) => {
    const x = e.target.closest("[data-exclude]");
    if (x) { toggleExcluded(Number(x.dataset.exclude)); return; }
    const row = e.target.closest(".conn");
    if (!row) return;
    setEdgeHover(null);
    if (row.dataset.kind === "hut") { selectHut(Number(row.dataset.place)); return; }
    const data = connections.get(`${state.hut}:${state.limit}`);
    const stop = data?.stops.find((s) => s.place.id === Number(row.dataset.place));
    if (stop) map.flyTo({ center: [stop.place.lon, stop.place.lat], zoom: Math.max(map.getZoom(), 13), padding: mapPadding() });
  });

  $("#base").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.dataset.v === state.base) return;
    setState({ base: b.dataset.v });
    syncControls();
    map.setStyle(STYLES[state.base], { diff: false });
  });
  $("#trails").addEventListener("click", () => {
    setState({ trails: !state.trails });
    syncControls();
    if (map.getLayer("trails")) map.setLayoutProperty("trails", "visibility", state.trails ? "visible" : "none");
  });

  $("#sheetHandle").addEventListener("click", () => {
    const next = { peek: "half", half: "full", full: "peek" }[$("#panel").dataset.sheet];
    setSheet(next);
  });
  $("#q").addEventListener("focus", () => { if (isMobile()) setSheet("full"); });

  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== $("#q")) {
      e.preventDefault();
      if (state.hut) closeDetail();
      $("#q").focus();
    } else if (e.key === "Escape") {
      if (state.hut) closeDetail();
      else if (state.tour) closeTour();
      else if (document.activeElement === $("#q")) $("#q").blur();
    }
  });
  window.addEventListener("hashchange", () => {
    const next = readHash();
    const hutChanged = next.hut !== state.hut;
    state = next;
    update();
    if (hutChanged) state.hut ? selectHut(state.hut) : closeDetail();
  });
}

function initMap() {
  map = new maplibregl.Map({
    container: "map",
    style: STYLES[state.base],
    bounds: CH_BOUNDS,
    fitBoundsOptions: { padding: mapPadding() },
    maxBounds: [[4.5, 45.2], [11.9, 48.3]],
    attributionControl: false,
    dragRotate: false,
    pitchWithRotate: false,
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: ATTRIBUTION }), "bottom-right");
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: false } }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

  map.on("style.load", addHutLayers);

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
      .setHTML(`${esc(f.properties.name)}<small>${f.properties.altitude ? `${f.properties.altitude} m` : ""}` +
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
  map.on("click", "huts", (e) => selectHut(e.features[0].id, { fly: false }));
}

// ---------- Start ----------
async function main() {
  renderControls();
  bindEvents();
  initMap();
  const res = await fetch("/api/huts");
  huts = await res.json();
  update();
  syncTourForm();
  setView(state.view);
  if (state.view === "tours" && state.tour) await runTourSearch();
  if (state.hut) selectHut(state.hut);
}

main().catch((err) => {
  console.error(err);
  $("#list").innerHTML = `<li class="empty">Could not load huts. Is the server running?</li>`;
});

// Connections of the selected hut (edges of the place graph): list in the detail view, lines on the map, review.
import { $, CATEGORY, EMPTY, categoryColor, decodePolyline, esc, formatMinutes, modeLabel } from "./util.js";
import { state } from "./state.js";
import { formatDistance, formatHeight, settingsQuery } from "./settings.js";
import { map, mapPadding } from "./map.js";
import { selectHut } from "./huts.js";

const STOPS_SHOWN = 8;
const LINES_SHOWN = 4;
const cache = new Map(); // "hutId:settings" → API response (the settings decide which connections exist)

/** Lines at a stop (most frequent first) and its summer departures per day, from the timetable. */
function linesMeta(place) {
  if (!place.lines) return "";
  const more = place.lines.length > LINES_SHOWN ? ` +${place.lines.length - LINES_SHOWN}` : "";
  return `<div class="conn-meta" title="Lines and departures per day in July and August (official timetable)">` +
    `${esc(place.lines.slice(0, LINES_SHOWN).join(", "))}${more} · ${place.departures}/day</div>`;
}

const allEdges = (data) => [...data.huts, ...data.stops.flatMap((s) => [s.to_hut, s.from_hut].filter(Boolean))];

/** The "Connections" section of the hut detail view; filled by showConnections(). */
export function connectionsSection(d) {
  const warning = d.snap_m > 500
    ? `<p class="warn">This hut is ${formatDistance(d.snap_m)} from the nearest signposted hiking trail. It is reached
       by alpine routes or across glaciers, so no walking connections are computed.</p>`
    : d.snap_m > 200
      ? `<p class="warn">This hut is ${formatHeight(d.snap_m)} from the nearest signposted trail, so connection times may be
         inaccurate.</p>` : "";
  return `<div class="section" id="connections">
      <h3>Connections</h3>
      <p class="hint">Within your settings (⚙: hardest trail, highest point, lifts). Walking times use the Swiss
        hiking-time formula at your pace, without breaks.</p>
      ${warning}
      <div id="connList"><p class="hint">Loading…</p></div>
    </div>`;
}

async function load(hutId, query) {
  const key = `${hutId}:${query}`;
  if (!cache.has(key)) {
    const res = await fetch(`/api/huts/${hutId}/edges?${query}`);
    if (!res.ok) throw new Error(`edges ${hutId}: ${res.status}`);
    cache.set(key, await res.json());
  }
  return cache.get(key);
}

const current = () => cache.get(`${state.hut}:${settingsQuery()}`);

export async function showConnections(hutId) {
  const query = settingsQuery();
  let data;
  try {
    data = await load(hutId, query);
  } catch (err) {
    console.error(err);
    if ($("#connList")) $("#connList").innerHTML = `<p class="hint">Could not load connections.</p>`;
    return;
  }
  if (state.hut !== hutId || settingsQuery() !== query || !$("#connList")) return;
  render(data);
  draw(data);
}

function edgeRow(e, label, kind, extra = "") {
  const cat = CATEGORY[e.max_category];
  const meta = [formatDistance(e.distance_m), `↑ ${formatHeight(e.ascent_m)}`, `↓ ${formatHeight(e.descent_m)}`];
  const sac = e.sac_time_min ? `<span class="sac" title="Time given by the SAC for this route">SAC ${formatMinutes(e.sac_time_min)}` +
    `${e.sac_grade ? ` · ${esc(e.sac_grade)}` : ""}</span>` : "";
  return `<li class="conn${e.excluded ? " excluded" : ""}" data-edge="${e.id}" data-place="${e.place.id}" data-kind="${kind}">
    <i class="cat-bar" style="background:${cat.color}" title="${cat.name}"></i>
    <div class="conn-main"><div class="conn-name">${label}</div>
      <div class="conn-meta">${meta.join(" · ")} ${sac}</div>${extra}</div>
    <div class="conn-time">${formatMinutes(e.time_min)}</div>
    <button class="conn-x" data-exclude="${e.id}" title="${e.excluded ? "Include this connection again" : "Exclude this connection (bad route)"}"
      aria-label="${e.excluded ? "Include" : "Exclude"}">${e.excluded ? "↺" : "×"}</button>
  </li>`;
}

function render(data, showAllStops = false) {
  const hutRows = data.huts.map((e) => edgeRow(e, esc(e.place.name), "hut"));
  const stopRows = [];
  for (const s of showAllStops ? data.stops : data.stops.slice(0, STOPS_SHOWN)) {
    for (const [dir, arrow] of [["to_hut", "from"], ["from_hut", "to"]]) {
      const e = s[dir];
      if (e) stopRows.push(edgeRow({ ...e, place: s.place },
        `<span class="mode">${modeLabel(s.place.mode)}</span> ${arrow} ${esc(s.place.name)}`, "stop",
        linesMeta(s.place)));
    }
  }
  $("#connList").innerHTML = `
    <h4>Huts within a day <span>${data.huts.length}</span></h4>
    ${hutRows.length ? `<ul class="conns">${hutRows.join("")}</ul>` : `<p class="hint">No huts within 10 hours on these trails.</p>`}
    <h4>Public transport <span>${data.stops.length}</span></h4>
    ${stopRows.length ? `<ul class="conns">${stopRows.join("")}</ul>` : `<p class="hint">No stops within 10 hours on these trails.</p>`}
    ${!showAllStops && data.stops.length > STOPS_SHOWN ? `<button class="link" id="allStops">Show all ${data.stops.length} stops</button>` : ""}`;
  $("#allStops")?.addEventListener("click", () => render(data, true));
}

function draw(data) {
  if (!map.getSource("edges")) return;
  map.getSource("edges").setData({
    type: "FeatureCollection",
    features: allEdges(data).map((e) => ({
      type: "Feature", id: e.id,
      geometry: { type: "LineString", coordinates: decodePolyline(e.geometry) },
      properties: { category: e.max_category, excluded: e.excluded },
    })),
  });
  map.getSource("edge-stops").setData({
    type: "FeatureCollection",
    features: data.stops.map((s) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [s.place.lon, s.place.lat] }, properties: { name: s.place.name },
    })),
  });
}

/** Redraw the selected hut's connections (after a base-style reload). */
export function redrawConnections() {
  const data = state.hut && current();
  if (data) draw(data);
}

export function clearConnections() {
  map.getSource("edges")?.setData(EMPTY);
  map.getSource("edge-stops")?.setData(EMPTY);
}

async function toggleExcluded(edgeId) {
  const data = current();
  const edge = allEdges(data).find((e) => e.id === edgeId);
  const res = await fetch(`/api/edges/${edgeId}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ excluded: !edge.excluded }),
  });
  if (!res.ok) {
    $("#connList").insertAdjacentHTML("afterbegin", `<p class="warn">${esc(await errorText(res))}</p>`);
    return;
  }
  const { excluded } = await res.json();
  for (const cached of cache.values()) {  // the same edge can appear under several limits
    for (const e of allEdges(cached)) if (e.id === edgeId) e.excluded = excluded;
  }
  render(data);
  draw(data);
}

/** Readable message for a refused change (401: password prompt cancelled or wrong, 403: no password set). */
export async function errorText(res) {
  if (res.status === 401) return "Changing data needs the admin password.";
  try {
    return (await res.json()).detail || `Could not save (${res.status}).`;
  } catch {
    return `Could not save (${res.status}).`;
  }
}

export function addEdgeLayers() {
  map.addSource("edges", { type: "geojson", data: EMPTY });
  map.addSource("edge-stops", { type: "geojson", data: EMPTY });
  const hover = (on, off) => ["case", ["boolean", ["feature-state", "hover"], false], on, off];
  map.addLayer({
    id: "edges-casing", type: "line", source: "edges", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#ffffff", "line-opacity": 0.85, "line-width": hover(9, 5) },
  });
  // MapLibre 4 has no data-driven dash patterns, so excluded edges get their own dashed layer.
  const edgePaint = { "line-color": categoryColor(), "line-width": hover(5, 2.5) };
  map.addLayer({
    id: "edges", type: "line", source: "edges", filter: ["!", ["get", "excluded"]],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { ...edgePaint, "line-opacity": hover(1, 0.8) },
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
}

export function bindConnectionEvents() {
  let hovered = null;
  const setHover = (id) => {
    if (hovered === id || !map.getSource("edges")) return;
    if (hovered) map.setFeatureState({ source: "edges", id: hovered }, { hover: false });
    hovered = id;
    if (id) map.setFeatureState({ source: "edges", id }, { hover: true });
  };
  const detail = $("#detail");
  detail.addEventListener("mouseover", (e) => setHover(Number(e.target.closest(".conn")?.dataset.edge) || null));
  detail.addEventListener("mouseleave", () => setHover(null));
  detail.addEventListener("click", (e) => {
    const x = e.target.closest("[data-exclude]");
    if (x) { toggleExcluded(Number(x.dataset.exclude)); return; }
    const row = e.target.closest(".conn");
    if (!row) return;
    setHover(null);
    if (row.dataset.kind === "hut") { selectHut(Number(row.dataset.place)); return; }
    const stop = current()?.stops.find((s) => s.place.id === Number(row.dataset.place));
    if (stop) map.flyTo({ center: [stop.place.lon, stop.place.lat], zoom: Math.max(map.getZoom(), 13), padding: mapPadding() });
  });
}

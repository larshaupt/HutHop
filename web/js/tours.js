// Tour planner: form, search, result cards, tour detail, GPX export and tour drawing on the map.
import { $, CATEGORY, EMPTY, categoryColor, decodePolyline, esc, formatMinutes, modeLabel, revealPanel } from "./util.js";
import { setState, state } from "./state.js";
import { fitCoords, map, visibleBox } from "./map.js";
import { selectHut } from "./huts.js";
import { clearConnections } from "./connections.js";

const NIGHT_LABEL = { catered: "Catered", self_service: "Self-service", unknown: "No opening data", closed: "Closed" };
const AVAIL_LABEL = {
  available: (a) => `${a.free_beds} beds free`,
  full: (a) => `Full (${a.free_beds ?? 0} beds free)`,
  closed: () => "Closed",
  not_released: () => "Not bookable yet",
  no_booking: () => "No online booking",
  not_checked: () => "Free beds not checked",
  error: () => "Could not check free beds",
};
const DAY_FMT = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" });
let results = null;
const stopSearch = { timer: null, seq: 0 };

const selectedTour = () => results?.tours[state.tour - 1];
const formatDay = (iso) => DAY_FMT.format(new Date(`${iso}T12:00:00`));

function todayIso() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

/** Next Saturday, or the first Saturday from 20 June when today is outside the summer season. */
function defaultTourDate() {
  const d = new Date();
  if (d.getMonth() < 5 || d.getMonth() > 8) d.setFullYear(d.getMonth() > 8 ? d.getFullYear() + 1 : d.getFullYear(), 5, 20);
  d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7));
  return d.toISOString().slice(0, 10);
}

// ---------- Form ----------
export function syncTourForm() {
  if (!state.tDate || state.tDate < todayIso()) state.tDate = defaultTourDate();
  $("#tDate").min = todayIso();
  $("#tDate").value = state.tDate;
  $("#tNightsLabel").textContent = state.tNights;
  $("#tPeopleLabel").textContent = state.tPeople;
  $("#tMin").value = state.tMin;
  $("#tMax").value = state.tMax;
  $("#tHoursLabel").textContent = `${state.tMin}–${state.tMax} h`;
  $("#tHoursFill").style.left = `${((state.tMin - 1) / 9) * 100}%`;
  $("#tHoursFill").style.right = `${100 - ((state.tMax - 1) / 9) * 100}%`;
  document.querySelectorAll("#tLimit button").forEach((b) => b.setAttribute("aria-pressed", Number(b.dataset.v) === state.limit));
  document.querySelectorAll("#tCat button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.v === state.tCat));
  document.querySelectorAll("#tLoop button").forEach((b) => b.setAttribute("aria-pressed", (b.dataset.v === "1") === state.tLoop));
  $("#tLoopField").hidden = !state.tStart;
  $("#tAvail").checked = state.tAvail;
  $("#tUnknown").checked = state.tUnknown;
  $("#tStartPicked").hidden = !state.tStart;
  $("#tStartInput").hidden = !!state.tStart;
  $("#tStartName").textContent = state.tStartName;
  $("#tAreaHint").hidden = !!state.tStart;
}

export function bindTourForm() {
  $("#tDate").addEventListener("change", (e) => {
    setState({ tDate: e.target.value && e.target.value >= todayIso() ? e.target.value : defaultTourDate() });
    syncTourForm();
  });
  const stepper = (attr, key, max) => document.querySelectorAll(`[${attr}]`).forEach((b) => b.addEventListener("click", () => {
    setState({ [key]: Math.min(max, Math.max(1, state[key] + Number(b.getAttribute(attr)))) });
    syncTourForm();
  }));
  stepper("data-step", "tNights", 10);
  stepper("data-people", "tPeople", 30);
  $("#tMin").addEventListener("input", (e) => { setState({ tMin: Math.min(Number(e.target.value), state.tMax - 0.5) }); syncTourForm(); });
  $("#tMax").addEventListener("input", (e) => { setState({ tMax: Math.max(Number(e.target.value), state.tMin + 0.5) }); syncTourForm(); });
  for (const [id, key, parse] of [["#tLimit", "limit", Number], ["#tCat", "tCat", String], ["#tLoop", "tLoop", (v) => v === "1"]]) {
    $(id).addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) { setState({ [key]: parse(b.dataset.v) }); syncTourForm(); }
    });
  }
  $("#tAvail").addEventListener("change", (e) => setState({ tAvail: e.target.checked }));
  $("#tUnknown").addEventListener("change", (e) => setState({ tUnknown: e.target.checked }));
  bindStopPicker();

  $("#tourForm").addEventListener("submit", (e) => {
    e.preventDefault();
    // Without a start point, search the area visible on the map; with one, search around it.
    setState({ tBox: state.tStart ? "" : visibleBox().map((v) => v.toFixed(3)).join(","), tour: 0 });
    runTourSearch();
  });

  $("#tourResults").addEventListener("click", (e) => {
    const card = e.target.closest(".tour-card");
    if (card) selectTour(Number(card.dataset.n));
  });
  $("#tourResults").addEventListener("mouseover", (e) => {
    const card = e.target.closest(".tour-card");
    if (card && results) drawTour(results.tours[Number(card.dataset.n) - 1], { fit: false });
  });
  $("#tourResults").addEventListener("mouseleave", () => clearTour());
  $("#tourDetail").addEventListener("click", (e) => {
    if (e.target.closest("#tourBack")) { closeTour(); return; }
    const hut = e.target.closest("[data-hut]");
    if (hut) { e.preventDefault(); selectHut(Number(hut.dataset.hut)); return; }
    const day = e.target.closest("[data-day]");
    if (day && !e.target.closest("a")) focusDay(Number(day.dataset.day));
  });
}

function bindStopPicker() {
  const input = $("#tStartInput");
  const list = $("#tStartList");
  input.addEventListener("input", () => {
    clearTimeout(stopSearch.timer);
    const q = input.value.trim();
    if (q.length < 2) { list.hidden = true; return; }
    stopSearch.timer = setTimeout(async () => {
      const seq = ++stopSearch.seq;
      const res = await fetch(`/api/stops?q=${encodeURIComponent(q)}`);
      const stops = res.ok ? await res.json() : [];
      if (seq !== stopSearch.seq) return;
      list.innerHTML = stops.map((s) => `<li role="option" data-id="${s.id}" data-name="${esc(s.name)}">` +
        `<span class="mode">${modeLabel(s.mode)}</span> ${esc(s.name)}</li>`).join("") || `<li class="none">No stop found</li>`;
      list.hidden = false;
    }, 180);
  });
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-id]");
    if (!li) return;
    e.preventDefault();
    setState({ tStart: Number(li.dataset.id), tStartName: li.dataset.name });
    input.value = "";
    list.hidden = true;
    syncTourForm();
  });
  input.addEventListener("blur", () => setTimeout(() => { list.hidden = true; }, 100));
  $("#tStartClear").addEventListener("click", () => {
    setState({ tStart: 0, tStartName: "", tLoop: false });
    syncTourForm();
    input.focus();
  });
}

// ---------- Search ----------
export async function runTourSearch() {
  const body = {
    start_date: state.tDate, nights: state.tNights, min_hours: state.tMin, max_hours: state.tMax,
    limit: state.limit, catering: state.tCat, loop: state.tLoop && !!state.tStart,
    include_unknown: state.tUnknown, start_stop: state.tStart || null,
    people: state.tPeople, check_availability: state.tAvail,
    bbox: state.tBox ? state.tBox.split(",").map(Number) : null,
  };
  const button = $("#tFind");
  button.disabled = true;
  button.textContent = state.tAvail ? "Searching and checking free beds…" : "Searching…";
  $("#tourResults").innerHTML = "";
  try {
    const res = await fetch("/api/tours/suggest", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (res.status === 422) {
      const detail = (await res.json()).detail;
      $("#tourResults").innerHTML = `<p class="empty">${esc(detail?.[0]?.msg?.replace("Value error, ", "") || "Invalid search.")}</p>`;
      return;
    }
    if (!res.ok) throw new Error(`${res.status}`);
    results = await res.json();
  } catch (err) {
    console.error(err);
    $("#tourResults").innerHTML = `<p class="empty">Search failed. Is the server running?</p>`;
    return;
  } finally {
    button.disabled = false;
    button.textContent = "Find tours";
  }
  renderResults();
  if (selectedTour()) selectTour(state.tour);
}

function renderResults() {
  const tours = results.tours;
  if (!tours.length) {
    $("#tourResults").innerHTML = `<div class="empty"><b>No tours found.</b><br>Try a wider range of hours, a harder trail,
      fewer nights, ${state.tStart ? "another start, " : "a larger map area, "}or including huts without opening data.</div>`;
    return;
  }
  $("#tourResults").innerHTML = `<div class="results-head"><span><strong>${tours.length}</strong> tours</span>
    <span class="hint">best match first</span></div>` + tours.map(tourCard).join("");
}

function tourCard(t, i) {
  const chain = [t.days[0].from, ...t.days.slice(0, -1).map((d) => d.to), t.days[t.days.length - 1].to];
  const longest = Math.max(...t.days.map((d) => d.time_min));
  const bars = t.days.map((d) => `<div class="daybar" title="Day ${d.day}: ${formatMinutes(d.time_min)} · ${CATEGORY[d.max_category].name}">
      <i style="height:${Math.max(18, (d.time_min / longest) * 100)}%;background:${CATEGORY[d.max_category].color}"></i>
      <span>${(d.time_min / 60).toFixed(1).replace(".0", "")}h</span></div>`).join("");
  return `<article class="tour-card" data-n="${i + 1}" tabindex="0">
    <div class="tour-head">
      <div><b>${t.nights} night${t.nights > 1 ? "s" : ""}</b> · ${(t.distance_m / 1000).toFixed(0)} km · ↑ ${t.ascent_m.toLocaleString("de-CH")} m</div>
      ${availBadge(t)}
    </div>
    <div class="tour-body">
      <ol class="chain">${chainItems(chain)}</ol>
      <div class="daybars${t.days.length > 6 ? " dense" : ""}" style="--days:${t.days.length}">${bars}</div>
    </div>
  </article>`;
}

/** Card badge: free beds on every night, or how many nights are not confirmed. */
function availBadge(t) {
  const a = t.availability || {};
  const checked = results?.query?.check_availability;
  if (checked && a.available === t.nights) return `<span class="flag ok" title="Free beds on every night">Beds free</span>`;
  const unsure = checked ? t.nights - (a.available || 0) : t.uncertain_nights;
  if (!unsure) return "";
  const title = checked ? "Nights without confirmed free beds (not bookable yet, no online booking or not checked)"
    : "Opening data missing or only part of the month";
  return `<span class="flag" title="${title}">${unsure} night${unsure > 1 ? "s" : ""} unconfirmed</span>`;
}

/** Start, huts and end of a tour card; long tours show the first two huts, "+ N more huts" and the last hut. */
function chainItems(chain) {
  const item = (p, end) => `<li class="${p.kind}">${end ? `<span class="mode">${modeLabel(p.mode)}</span> ` : ""}${esc(p.name)}</li>`;
  const huts = chain.slice(1, -1);
  const shown = huts.length > 4
    ? [...huts.slice(0, 2).map((p) => item(p)), `<li class="more">+ ${huts.length - 3} more huts</li>`, item(huts[huts.length - 1])]
    : huts.map((p) => item(p));
  return [item(chain[0], true), ...shown, item(chain[chain.length - 1], true)].join("");
}

// ---------- Tour detail ----------
function selectTour(n) {
  const t = results?.tours[n - 1];
  if (!t) return;
  setState({ tour: n });
  renderTourDetail(t);
  $("#browse").hidden = true;
  $("#detail").hidden = true;
  $("#tourDetail").hidden = false;
  $("#tourDetail").scrollTop = 0;
  clearConnections();
  drawTour(t);
  revealPanel();
}

export function closeTour() {
  setState({ tour: 0 });
  clearTour();
  $("#tourDetail").hidden = true;
  $("#browse").hidden = false;
}

/** Back from a hut opened inside a tour: return to that tour. */
export function returnToTour() {
  const t = selectedTour();
  if (!t) return false;
  $("#tourDetail").hidden = false;
  drawTour(t, { fit: false });
  return true;
}

function stopLabel(p) {
  return `<span class="mode">${modeLabel(p.mode)}</span> ${esc(p.name)}`;
}

function renderTourDetail(t) {
  const start = t.days[0].from, end = t.days[t.days.length - 1].to;
  const rows = t.days.map((d) => {
    const cat = CATEGORY[d.max_category];
    const sac = d.sac_time_min ? ` · SAC ${formatMinutes(d.sac_time_min)}` : "";
    const from = d.from.kind === "stop" ? stopLabel(d.from) : esc(d.from.name);
    const to = d.to.kind === "stop" ? stopLabel(d.to) : `<a href="#" data-hut="${d.to.id}">${esc(d.to.name)}</a>`;
    const night = d.night ? `<li class="tl-night">
        <div class="tl-dot hut"><span>${d.day}</span></div>
        <div><b><a href="#" data-hut="${d.to.id}">${esc(d.to.name)}</a></b>
          <div class="tl-meta">Night of ${formatDay(d.date)} ·
            <span class="night ${d.night.status}">${NIGHT_LABEL[d.night.status]}${d.night.partial ? ", part of the month – check dates" : ""}</span></div>
          ${nightAvailability(d.night)}
        </div></li>` : "";
    return `<li class="tl-day" data-day="${d.day}">
        <div class="tl-dot" style="--c:${cat.color}"></div>
        <div><div class="tl-title">Day ${d.day} · ${formatDay(d.date)}</div>
          <div>${from} → ${to}</div>
          <div class="tl-meta"><b>${formatMinutes(d.time_min)}</b> · ${(d.distance_m / 1000).toFixed(1)} km ·
            ↑ ${d.ascent_m} m · ↓ ${d.descent_m} m · <span style="color:${cat.color}">${cat.name}</span>${sac}</div>
        </div></li>${night}`;
  }).join("");
  $("#tourDetail").innerHTML = `
    <div class="tour-top">
      <button class="back flat" id="tourBack"><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>Tours</button>
      <h2>${esc(start.name)} → ${esc(end.name)}</h2>
      <div class="subtitle">${t.nights} night${t.nights > 1 ? "s" : ""}, ${t.days.length} days ·
        ${formatDay(t.days[0].date)} – ${formatDay(t.days[t.days.length - 1].date)}</div>
      <div class="facts">
        <div class="fact"><b>${formatMinutes(t.time_min).replace(" h", "")}</b><span>hours walking</span></div>
        <div class="fact"><b>${(t.distance_m / 1000).toFixed(1)}</b><span>km</span></div>
        <div class="fact"><b>${t.ascent_m.toLocaleString("de-CH")}</b><span>m ascent</span></div>
      </div>
      <div class="actions">
        <a class="btn" href="/api/tours/${encodeURIComponent(t.id)}/gpx" download>Download GPX</a>
      </div>
    </div>
    <div class="detail-body">
      <ol class="timeline">${rows}</ol>
      <p class="source">Walking times use the Swiss hiking-time formula without breaks. Opening data from the SAC;
        free beds from hut-reservation.org (cached up to 10 minutes; not a reservation). Check conditions, weather
        and official sources before you go.</p>
    </div>`;
}

function nightAvailability(n) {
  const a = n.availability;
  if (!a) return "";
  const ago = a.checked_at ? Math.max(0, Math.round((Date.now() - new Date(a.checked_at)) / 60000)) : null;
  const when = ago === null ? "" : ago < 1 ? " · checked just now" : ` · checked ${ago} min ago`;
  const book = n.booking_url ? ` <a class="book" href="${esc(n.booking_url)}" target="_blank" rel="noopener">Book</a>` : "";
  return `<div class="tl-meta avail ${a.state}">${AVAIL_LABEL[a.state](a)}${a.state === "available" ? when : ""}${book}</div>`;
}

// ---------- Map ----------
function drawTour(t, { fit = true } = {}) {
  if (!map.getSource("tour") || !t) return;
  const lines = t.days.map((d) => ({
    type: "Feature", id: d.day,
    geometry: { type: "LineString", coordinates: decodePolyline(d.geometry || "") },
    properties: { day: d.day, category: d.max_category },
  }));
  const points = [
    { ...t.days[0].from, label: "", role: "end" },
    ...t.days.slice(0, -1).map((d) => ({ ...d.to, label: String(d.day), role: "hut" })),
    { ...t.days[t.days.length - 1].to, label: "", role: "end" },
  ].map((p) => ({ type: "Feature", geometry: { type: "Point", coordinates: [p.lon, p.lat] },
    properties: { label: p.label, role: p.role, name: p.name } }));
  map.getSource("tour").setData({ type: "FeatureCollection", features: lines });
  map.getSource("tour-points").setData({ type: "FeatureCollection", features: points });
  if (fit) fitCoords(lines.flatMap((l) => l.geometry.coordinates));
}

function focusDay(day) {
  const t = selectedTour();
  if (t) fitCoords(decodePolyline(t.days[day - 1].geometry || ""), 14);
}

/** Remove a hovered preview; keeps the selected tour drawn while its detail is open. */
export function clearTour() {
  map?.getSource("tour")?.setData(EMPTY);
  map?.getSource("tour-points")?.setData(EMPTY);
  if (!$("#tourDetail").hidden) drawTour(selectedTour(), { fit: false });
}

export function redrawTour() {
  if (state.tour && !$("#tourDetail").hidden) drawTour(selectedTour(), { fit: false });
}

export function hasResults() {
  return !!results;
}

export function addTourLayers() {
  map.addSource("tour", { type: "geojson", data: EMPTY });
  map.addSource("tour-points", { type: "geojson", data: EMPTY });
  map.addLayer({
    id: "tour-casing", type: "line", source: "tour", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#ffffff", "line-width": 8, "line-opacity": 0.9 },
  });
  map.addLayer({
    id: "tour-line", type: "line", source: "tour", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": categoryColor(), "line-width": 4.5 },
  });
  map.addLayer({
    id: "tour-points", type: "circle", source: "tour-points",
    paint: {
      "circle-radius": ["match", ["get", "role"], "hut", 11, 6],
      "circle-color": ["match", ["get", "role"], "hut", "#1d2521", "#ffffff"],
      "circle-stroke-color": "#1d2521", "circle-stroke-width": 2,
    },
  });
  map.addLayer({
    id: "tour-labels", type: "symbol", source: "tour-points",
    layout: { "text-field": ["get", "label"], "text-font": ["Frutiger Neue Condensed Bold"], "text-size": 12,
      "text-allow-overlap": true, "text-ignore-placement": true },
    paint: { "text-color": "#ffffff" },
  });
  map.addLayer({
    id: "tour-names", type: "symbol", source: "tour-points", minzoom: 9,
    layout: { "text-field": ["get", "name"], "text-font": ["Frutiger Neue Condensed Bold"], "text-size": 12,
      "text-variable-anchor": ["left", "right", "top", "bottom"], "text-radial-offset": 1.2, "text-optional": true },
    paint: { "text-color": "#1d2521", "text-halo-color": "#ffffff", "text-halo-width": 1.8 },
  });
}

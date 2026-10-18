"use strict";
// Tour planner: form, search, result cards, tour detail and tour drawing on the map.
// Loaded before app.js; uses its globals ($, esc, state, setState, map, …) only inside functions.

const NIGHT_LABEL = { catered: "Catered", self_service: "Self-service", unknown: "No opening data" };
const DAY_FMT = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" });
let tourResults = null;
let stopSearch = { timer: null, seq: 0 };

/** Next Saturday, or the first Saturday from 20 June when today is outside the summer season. */
function defaultTourDate() {
  const d = new Date();
  if (d.getMonth() < 5 || d.getMonth() > 8) {
    const year = d.getMonth() > 8 ? d.getFullYear() + 1 : d.getFullYear();
    d.setFullYear(year, 5, 20);
  }
  d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7));
  return d.toISOString().slice(0, 10);
}

function formatDay(iso) {
  return DAY_FMT.format(new Date(`${iso}T12:00:00`));
}

// ---------- Form ----------
function todayIso() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function syncTourForm() {
  if (!state.tDate || state.tDate < todayIso()) state.tDate = defaultTourDate();
  $("#tDate").min = todayIso();
  $("#tDate").value = state.tDate;
  $("#tNightsLabel").textContent = state.tNights;
  $("#tMin").value = state.tMin;
  $("#tMax").value = state.tMax;
  $("#tHoursLabel").textContent = `${state.tMin}–${state.tMax} h`;
  $("#tHoursFill").style.left = `${((state.tMin - 1) / 9) * 100}%`;
  $("#tHoursFill").style.right = `${100 - ((state.tMax - 1) / 9) * 100}%`;
  document.querySelectorAll("#tLimit button").forEach((b) => b.setAttribute("aria-pressed", Number(b.dataset.v) === state.limit));
  document.querySelectorAll("#tCat button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.v === state.tCat));
  document.querySelectorAll("#tLoop button").forEach((b) => b.setAttribute("aria-pressed", (b.dataset.v === "1") === state.tLoop));
  $("#tLoop").style.opacity = state.tStart ? 1 : 0.5;
  $("#tLoop").title = state.tStart ? "" : "Back to start means: end within 3 km of where the tour starts";
  $("#tArea").checked = state.tArea;
  $("#tUnknown").checked = state.tUnknown;
  $("#tStartPicked").hidden = !state.tStart;
  $("#tStartInput").hidden = !!state.tStart;
  $("#tStartName").textContent = state.tStartName;
}

function bindTourForm() {
  $("#tDate").addEventListener("change", (e) => {
    setState({ tDate: e.target.value && e.target.value >= todayIso() ? e.target.value : defaultTourDate() });
    syncTourForm();
  });
  document.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => {
    setState({ tNights: Math.min(10, Math.max(1, state.tNights + Number(b.dataset.step))) });
    syncTourForm();
  }));
  $("#tMin").addEventListener("input", (e) => {
    setState({ tMin: Math.min(Number(e.target.value), state.tMax - 0.5) });
    syncTourForm();
  });
  $("#tMax").addEventListener("input", (e) => {
    setState({ tMax: Math.max(Number(e.target.value), state.tMin + 0.5) });
    syncTourForm();
  });
  for (const [id, key, parse] of [["#tLimit", "limit", Number], ["#tCat", "tCat", String], ["#tLoop", "tLoop", (v) => v === "1"]]) {
    $(id).addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) { setState({ [key]: parse(b.dataset.v) }); syncTourForm(); }
    });
  }
  $("#tArea").addEventListener("change", (e) => setState({ tArea: e.target.checked }));
  $("#tUnknown").addEventListener("change", (e) => setState({ tUnknown: e.target.checked }));

  // Start stop autocomplete
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
        `<span class="mode">${MODE_LABELS[mainMode(s.mode)]}</span> ${esc(s.name)}</li>`).join("") ||
        `<li class="none">No stop found</li>`;
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

  $("#tourForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const patch = { tBox: state.tArea ? visibleBox().map((v) => v.toFixed(3)).join(",") : "", tour: 0 };
    setState(patch);
    runTourSearch();
  });

  $("#tourResults").addEventListener("click", (e) => {
    const card = e.target.closest(".tour-card");
    if (card) selectTour(Number(card.dataset.n));
  });
  $("#tourResults").addEventListener("mouseover", (e) => {
    const card = e.target.closest(".tour-card");
    if (card && tourResults) drawTour(tourResults.tours[Number(card.dataset.n) - 1], { fit: false });
  });
  $("#tourResults").addEventListener("mouseleave", () => clearTour());
  $("#tourDetail").addEventListener("click", (e) => {
    if (e.target.closest("#tourBack")) { closeTour(); return; }
    const hut = e.target.closest("[data-hut]");
    if (hut) selectHut(Number(hut.dataset.hut));
    const day = e.target.closest("[data-day]");
    if (day && !hut) focusDay(Number(day.dataset.day));
  });
}

/** Map bounds of the area not covered by the panel, as [west, south, east, north]. */
function visibleBox() {
  const canvas = map.getCanvas().getBoundingClientRect();
  const panel = $("#panel").getBoundingClientRect();
  const left = isMobile() ? 0 : panel.right - canvas.left;
  const bottom = isMobile() ? panel.top - canvas.top : canvas.height;
  const sw = map.unproject([left, bottom]);
  const ne = map.unproject([canvas.width, 0]);
  return [sw.lng, sw.lat, ne.lng, ne.lat];
}

// ---------- Search ----------
async function runTourSearch() {
  const body = {
    start_date: state.tDate, nights: state.tNights, min_hours: state.tMin, max_hours: state.tMax,
    limit: state.limit, catering: state.tCat, loop: state.tLoop && !!state.tStart,
    include_unknown: state.tUnknown, start_stop: state.tStart || null,
    bbox: state.tBox ? state.tBox.split(",").map(Number) : null,
  };
  $("#tFind").disabled = true;
  $("#tFind").textContent = "Searching…";
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
    tourResults = await res.json();
  } catch (err) {
    console.error(err);
    $("#tourResults").innerHTML = `<p class="empty">Search failed. Is the server running?</p>`;
    return;
  } finally {
    $("#tFind").disabled = false;
    $("#tFind").textContent = "Find tours";
  }
  renderTourResults();
  if (state.tour && tourResults.tours[state.tour - 1]) selectTour(state.tour, { fit: true });
}

function renderTourResults() {
  const tours = tourResults.tours;
  if (!tours.length) {
    $("#tourResults").innerHTML = `<div class="empty">
      <b>No tours found.</b><br>Try a wider range of hours, a harder trail, fewer nights,
      ${state.tArea ? "a larger map area, " : ""}${state.tStart ? "another start, " : ""}or including huts without opening data.</div>`;
    return;
  }
  $("#tourResults").innerHTML = `<div class="results-head"><span><strong>${tours.length}</strong> tours</span>
    <span class="hint">best match first</span></div>` + tours.map(tourCard).join("");
}

function tourCard(t, i) {
  const first = t.days[0].from, last = t.days[t.days.length - 1].to;
  const chain = [first, ...t.days.slice(0, -1).map((d) => d.to), last];
  const longest = Math.max(...t.days.map((d) => d.time_min));
  const bars = t.days.map((d) => `<div class="daybar" title="Day ${d.day}: ${formatMinutes(d.time_min)}">
      <i style="height:${Math.max(18, (d.time_min / longest) * 100)}%;background:${CATEGORY[d.max_category].color}"></i>
      <span>${(d.time_min / 60).toFixed(1).replace(".0", "")}h</span></div>`).join("");
  return `<article class="tour-card" data-n="${i + 1}" tabindex="0">
    <div class="tour-head">
      <div><b>${t.nights} night${t.nights > 1 ? "s" : ""}</b> · ${(t.distance_m / 1000).toFixed(0)} km · ↑ ${t.ascent_m.toLocaleString("de-CH")} m</div>
      ${t.uncertain_nights ? `<span class="flag" title="Opening data missing or only part of the month">${t.uncertain_nights} uncertain</span>` : ""}
    </div>
    <div class="tour-body">
      <ol class="chain">${chain.map((p, k) => `<li class="${p.kind}">${k === 0 || k === chain.length - 1
        ? `<span class="mode">${MODE_LABELS[mainMode(p.mode)]}</span> ` : ""}${esc(p.name)}</li>`).join("")}</ol>
      <div class="daybars${t.days.length > 6 ? " dense" : ""}" style="--days:${t.days.length}">${bars}</div>
    </div>
  </article>`;
}

// ---------- Tour detail ----------
function selectTour(n, { fit = true } = {}) {
  const t = tourResults?.tours[n - 1];
  if (!t) return;
  setState({ tour: n });
  renderTourDetail(t);
  $("#browse").hidden = true;
  $("#detail").hidden = true;
  $("#tourDetail").hidden = false;
  $("#tourDetail").scrollTop = 0;
  clearConnections();
  drawTour(t, { fit });
  if (isMobile() && $("#panel").dataset.sheet === "peek") setSheet("half");
}

function closeTour() {
  setState({ tour: 0 });
  clearTour();
  $("#tourDetail").hidden = true;
  $("#browse").hidden = false;
}

/** Back from a hut opened inside a tour: return to that tour. */
function returnToTour() {
  const t = tourResults?.tours[state.tour - 1];
  if (!t) return false;
  $("#tourDetail").hidden = false;
  drawTour(t, { fit: false });
  return true;
}

function renderTourDetail(t) {
  const start = t.days[0].from, end = t.days[t.days.length - 1].to;
  const rows = t.days.map((d) => {
    const cat = CATEGORY[d.max_category];
    const sac = d.sac_time_min ? ` · SAC ${formatMinutes(d.sac_time_min)}` : "";
    const from = d.from.kind === "stop" ? `<span class="mode">${MODE_LABELS[mainMode(d.from.mode)]}</span> ${esc(d.from.name)}`
      : esc(d.from.name);
    const to = d.to.kind === "stop" ? `<span class="mode">${MODE_LABELS[mainMode(d.to.mode)]}</span> ${esc(d.to.name)}`
      : `<a href="#" data-hut="${d.to.id}">${esc(d.to.name)}</a>`;
    const night = d.night ? `<li class="tl-night">
        <div class="tl-dot hut"><span>${d.day}</span></div>
        <div><b><a href="#" data-hut="${d.to.id}">${esc(d.to.name)}</a></b>
          <div class="tl-meta">Night of ${formatDay(d.date)} ·
            <span class="night ${d.night.status}">${NIGHT_LABEL[d.night.status]}${d.night.partial ? ", part of the month – check dates" : ""}</span></div>
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
    </div>
    <div class="detail-body">
      <ol class="timeline">${rows}</ol>
      <p class="source">Walking times use the Swiss hiking-time formula without breaks. Opening data from the SAC;
        availability is not checked yet. Check conditions, weather and official sources before you go.</p>
    </div>`;
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
  if (fit) {
    const coords = lines.flatMap((l) => l.geometry.coordinates);
    const bounds = coords.reduce((b, c) => b.extend(c), new maplibregl.LngLatBounds(coords[0], coords[0]));
    map.fitBounds(bounds, { padding: mapPadding(), maxZoom: 13, duration: 800 });
  }
}

function focusDay(day) {
  const t = tourResults?.tours[state.tour - 1];
  const coords = t ? decodePolyline(t.days[day - 1].geometry || "") : [];
  if (!coords.length) return;
  const bounds = coords.reduce((b, c) => b.extend(c), new maplibregl.LngLatBounds(coords[0], coords[0]));
  map.fitBounds(bounds, { padding: mapPadding(), maxZoom: 14, duration: 600 });
}

function clearTour() {
  const empty = { type: "FeatureCollection", features: [] };
  map.getSource("tour")?.setData(empty);
  map.getSource("tour-points")?.setData(empty);
  if (state.tour && tourResults && !$("#tourDetail").hidden) drawTour(tourResults.tours[state.tour - 1], { fit: false });
}

function addTourLayers() {
  const empty = { type: "FeatureCollection", features: [] };
  map.addSource("tour", { type: "geojson", data: empty });
  map.addSource("tour-points", { type: "geojson", data: empty });
  map.addLayer({
    id: "tour-casing", type: "line", source: "tour", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#ffffff", "line-width": 8, "line-opacity": 0.9 },
  });
  map.addLayer({
    id: "tour-line", type: "line", source: "tour", layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": ["match", ["get", "category"], 1, CATEGORY[1].color, 2, CATEGORY[2].color, CATEGORY[3].color],
      "line-width": 4.5,
    },
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

// Tour planner: form, search, result cards (sorted here, without a new search), "My tours", opening a tour (a
// search result, or by id: my tours, shared links, the editor), map drawing.
import { $, CATEGORY, EMPTY, categoryColor, costTitle, decodePolyline, esc, formatCost, formatMinutes, modeLabel,
  revealPanel } from "./util.js";
import { setState, state } from "./state.js";
import { fitCoords, map, visibleBox } from "./map.js";
import { selectHut } from "./huts.js";
import { clearConnections } from "./connections.js";
import { loadInfo } from "./info.js";
import { renderTourDetail } from "./tour-detail.js";
import { findEntry, keepTour, onYourToursChange, removeEntry, yourTours } from "./your-tours.js";
import { isEditing, openEditor, refreshEditor } from "./tour-editor.js";

const SORTS = { best: "Best", time: "Shortest", ascent: "Least ascent", altitude: "Highest huts",
  beds: "Most free beds", cost: "Cheapest" };
// The search returns a pool of varied tours ordered by "best"; other orders are applied here, without a new search.
const SORT_KEYS = {
  best: (t) => [-t.score], time: (t) => [t.time_min, -t.score],
  ascent: (t) => [t.ascent_m, -t.score], altitude: (t) => [-(t.hut_altitude || 0), -t.score],
  beds: (t) => [-t.metrics.beds, -t.score],
  // Fully priced tours first (partial sums would look cheap), then by the per-person total
  cost: (t) => [t.cost.priced < t.nights ? 1 : 0,
    Object.values(t.cost.totals).reduce((sum, c) => sum + (state.tMember ? c.member : c.non_member), 0), -t.score],
};
const PAGE = 10;
let results = null;
let order = []; // results.tours in the chosen sort order; state.tour is a 1-based index into it
let current = null; // the open tour: a search result (state.tour) or one opened by id (state.tid)
let shown = PAGE;
let metricLabels = {};
const stopSearch = { timer: null, seq: 0 };

const selectedTour = () => current;
export const currentTour = () => current;
/** The editor's tour becomes the open tour (opened by id). */
export function setCurrent(t) {
  current = t;
  setState({ tour: 0, tid: t.id });
}

function sortResults() {
  const key = SORT_KEYS[state.tSort] || SORT_KEYS.best;
  const cmp = (a, b) => {
    const ka = key(a), kb = key(b);
    for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] - kb[i];
    return 0;
  };
  order = results ? results.tours.slice().sort(cmp) : [];
}

function todayIso() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

/** The coming Saturday (today if it is a Saturday), as a local date. */
function defaultTourDate() {
  const d = new Date();
  d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7));
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
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
  $("#tLifts").checked = state.tLifts;
  $("#tMember").checked = state.tMember;
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
  $("#tLifts").addEventListener("change", (e) => setState({ tLifts: e.target.checked }));
  // Member prices only change the shown estimate: no new search
  $("#tMember").addEventListener("change", (e) => {
    setState({ tMember: e.target.checked });
    if (!results) return;
    if (state.tSort === "cost") {  // member prices can change the order; keep the open tour selected
      const open = state.tour ? selectedTour() : null;
      sortResults();
      if (open) setState({ tour: order.indexOf(open) + 1 });
    }
    renderResults();
    if (selectedTour() && !$("#tourDetail").hidden && !isEditing()) renderTourDetail(selectedTour(), detailOptions());
  });
  bindStopPicker();
  bindYourTours();

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
    if (card && results) drawTour(order[Number(card.dataset.n) - 1], { fit: false });
  });
  $("#tourResults").addEventListener("mouseleave", () => clearTour());
  $("#tourDetail").addEventListener("click", (e) => {
    if (isEditing()) return;  // the editor handles its own clicks
    if (e.target.closest("#tourBack")) { closeTour(); return; }
    if (e.target.closest("#tourEdit")) { openEditor(current); return; }
    if (e.target.closest("#tourKeep")) {
      const entry = findEntry(current.id);
      entry ? removeEntry(entry.key) : keepTour(current);
      renderTourDetail(current, detailOptions());
      return;
    }
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
    include_unknown: state.tUnknown, start_stop: state.tStart || null, avoid_lifts: state.tLifts,
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
    // Only the criteria that count towards "best" (weight > 0) appear in the score tooltip.
    metricLabels = Object.fromEntries(((await loadInfo())?.metrics || []).filter((m) => m.weight > 0).map((m) => [m.key, m.label]));
  } catch (err) {
    console.error(err);
    $("#tourResults").innerHTML = `<p class="empty">Search failed. Is the server running?</p>`;
    return;
  } finally {
    button.disabled = false;
    button.textContent = "Find tours";
  }
  sortResults();
  shown = Math.max(PAGE, Math.ceil(state.tour / PAGE) * PAGE);
  renderResults();
  if (state.tour && order[state.tour - 1]) selectTour(state.tour);
}

function renderResults() {
  const tours = order;
  if (!tours.length) {
    $("#tourResults").innerHTML = `<div class="empty"><b>No tours found.</b><br>Try a wider range of hours, a harder trail,
      fewer nights, ${state.tStart ? "another start, " : "a larger map area, "}or including huts without opening data.</div>`;
    return;
  }
  $("#tourResults").innerHTML = `<div class="results-head"><span><strong>${tours.length}</strong> tours</span>
    <label class="sort">Sort <select id="tSort">${Object.entries(SORTS).map(([v, label]) =>
      `<option value="${v}"${v === state.tSort ? " selected" : ""}>${label}</option>`).join("")}</select></label></div>` +
    tours.slice(0, shown).map(tourCard).join("") +
    (tours.length > shown ? `<button class="btn wide" id="tMore">Show ${Math.min(PAGE, tours.length - shown)} more</button>` : "");
  $("#tSort").addEventListener("change", (e) => {
    setState({ tSort: e.target.value, tour: 0 });
    sortResults();
    shown = PAGE;
    renderResults();
  });
  $("#tMore")?.addEventListener("click", () => {
    shown += PAGE;
    renderResults();
  });
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
      <div class="tour-flags">${transitBadge(t)}${availBadge(t)}${costBadge(t)}${scoreBadge(t)}</div>
    </div>
    <div class="tour-body">
      <ol class="chain">${chainItems(chain)}</ol>
      <div class="daybars${t.days.length > 6 ? " dense" : ""}" style="--days:${t.days.length}">${bars}</div>
    </div>
  </article>`;
}

/** "Best" score as a percentage; the tooltip lists the sub-scores (weights come from huthop.toml). */
function scoreBadge(t) {
  if (!t.metrics) return "";
  const parts = Object.entries(metricLabels).map(([k, label]) => `${label}: ${Math.round(t.metrics[k] * 100)}%`).join("\n");
  return `<span class="score" title="Best score\n${parts}">${Math.round(t.score * 100)}%</span>`;
}

/** Card badge when the first connection reaches the first hut late, or the last one leaves early. */
function transitBadge(t) {
  const late = t.access?.late, early = t.exit?.early;
  if (!late && !early) return "";
  const why = [late && `first connection: at the hut from ${t.access.hut_from}`, early && `last connection: leave the last hut by ${t.exit.leave_by}`];
  return `<span class="flag" title="${why.filter(Boolean).join("; ")}">Tight connection</span>`;
}

/** Card badge: estimated hut costs per person ("from" when some nights have no prices). */
function costBadge(t) {
  if (!t.cost?.priced) return "";
  const from = t.cost.priced < t.nights ? "from " : "";
  return `<span class="flag pending" title="${esc(costTitle(t, state.tMember, state.tPeople))}">` +
    `${from}${formatCost(t.cost.totals, state.tMember)} p.p.</span>`;
}

/** Card badge: free beds on every night, or how many nights are not confirmed. */
function availBadge(t) {
  const a = t.availability || {};
  const checked = results?.query?.check_availability;
  if (checked && a.available === t.nights) return `<span class="flag ok" title="Free beds on every night">Beds free</span>`;
  // Beyond the first candidates of a search, free beds are only checked when the tour is opened.
  if (checked && a.not_checked) {
    return `<span class="flag pending" title="Free beds not checked yet for ${a.not_checked} night${a.not_checked > 1 ? "s" : ""}. Open the tour to check them.">Beds: open to check</span>`;
  }
  const unsure = checked ? t.nights - (a.available || 0) : t.uncertain_nights;
  if (!unsure) return "";
  const title = checked ? "Nights without confirmed free beds (not bookable yet or no online booking)"
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

// ---------- My tours (own tab) ----------
function bindYourTours() {
  renderYourTours();
  onYourToursChange(renderYourTours);
  $("#yoursList").addEventListener("click", (e) => {
    const remove = e.target.closest("[data-remove]");
    if (remove) { removeEntry(remove.dataset.remove); return; }
    const open = e.target.closest("[data-open]");
    if (open) openTourById(open.dataset.open);
  });
}

function renderYourTours() {
  const list = yourTours();
  $("#yoursCount").textContent = list.length ? list.length : "";
  $("#yoursList").innerHTML = list.length ? list.map((e) => `<li>
      <button class="your-tour" data-open="${esc(e.id)}">
        <b>${esc(e.title)}</b>
        <span>${esc(e.date)} · ${e.nights} night${e.nights > 1 ? "s" : ""} · ${esc(e.huts.join(", "))}</span>
      </button>
      <button class="conn-x" data-remove="${esc(e.key)}" title="Remove from my tours" aria-label="Remove">×</button>
    </li>`).join("") : `<li class="hint">No tours yet. Open a tour and choose “Add to my tours”, edit one, or plan one
      from a hut's details.</li>`;
}

// ---------- Tour detail ----------
const detailOptions = () => ({ kept: !!(current && findEntry(current.id)) });

function selectTour(n) {
  const t = order[n - 1];
  if (!t) return;
  current = t;
  setState({ tour: n, tid: "", tEdit: false });
  showTour(t);
}

/** A tour by id (my tours, a shared link, the editor's result), or null if it no longer exists. */
export async function openTourById(id, { edit = false } = {}) {
  const res = await fetch(`/api/tours/${encodeURIComponent(id)}?people=${state.tPeople}&limit=${state.limit}`);
  if (!res.ok) {
    $("#tourResults").insertAdjacentHTML("afterbegin", `<p class="warn">This tour is no longer available (a connection
      was removed or excluded).</p>`);
    return null;
  }
  current = await res.json();
  setState({ tour: 0, tid: id, tEdit: edit });
  if (edit) openEditor(current);
  else showTour(current);
  return current;
}

/** Show a tour in the detail view (also used by the editor for its result). */
export function showTour(t) {
  current = t;
  renderTourDetail(t, detailOptions());
  $("#browse").hidden = true;
  $("#detail").hidden = true;
  $("#tourDetail").hidden = false;
  $("#tourDetail").scrollTop = 0;
  clearConnections();
  drawTour(t);
  revealPanel();
  checkTourBeds(t);
}

/** Free beds of a tour's nights not checked yet (beyond a search's first candidates, or edited / opened by id):
 * checked when the tour is shown, if the search asks for free beds. */
export async function checkTourBeds(t) {
  const fromSearch = !!(state.tour && results);
  const check = fromSearch ? results.query.check_availability : state.tAvail;
  const people = fromSearch ? results.query.people : state.tPeople;
  if (!check || !t.days.some((d) => d.night && (!d.night.availability || d.night.availability.state === "not_checked"))) return;
  const res = await fetch(`/api/tours/${encodeURIComponent(t.id)}?people=${people}&check_availability=true&limit=${state.limit}`);
  if (!res.ok) return;
  const fresh = await res.json();
  // Keep score and order (computed for the search's hours); take only the nights' availability and warnings.
  t.days.forEach((d, i) => { d.night = fresh.days[i].night; });
  t.availability = fresh.availability;
  t.warnings = fresh.warnings;
  if (fromSearch) renderResults();  // the card's badge, for when the list is shown again
  if (current === t && !$("#tourDetail").hidden) {
    if (isEditing()) refreshEditor(t);
    else renderTourDetail(t, detailOptions());
  }
}

export function closeTour() {
  setState({ tour: 0, tid: "", tEdit: false });
  current = null;
  clearTour();
  $("#tourDetail").hidden = true;
  $("#browse").hidden = false;
}

/** Back from a hut opened inside a tour: return to that tour. */
export function returnToTour() {
  if (!current) return false;
  $("#tourDetail").hidden = false;
  drawTour(current, { fit: false });
  return true;
}

// ---------- Map ----------
export function drawTour(t, { fit = true } = {}) {
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
  if (current && !$("#tourDetail").hidden) drawTour(current, { fit: false });
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

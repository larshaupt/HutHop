// Tour planner: form, search, result cards (sorted here, without a new search), "My tours", opening a tour (a
// search result, or by id: my tours, shared links, the editor), map drawing.
import { $, CATEGORY, EMPTY, categoryColor, categoryName, costTitle, decodePolyline, esc, formatCost, formatMinutes, localIso,
  modeLabel, revealPanel, shareLink } from "./util.js";
import { setState, state } from "./state.js";
import { t as tr } from "./i18n.js";  // t is a tour here
import { distanceValue, distanceUnit, formatHeight, settings, settingsParams, settingsQuery } from "./settings.js";
import { fitCoords, map, visibleBox } from "./map.js";
import { selectHut } from "./huts.js";
import { clearConnections } from "./connections.js";
import { loadInfo } from "./info.js";
import { formatDay, renderTourDetail, toggleTimetable, tourSparkline } from "./tour-detail.js";
import { setBooked } from "./booking.js";
import { findEntry, keepTour, onYourToursChange, removeEntry, yourTours } from "./your-tours.js";
import { closeEditor, isEditing, openEditor, refreshEditor } from "./tour-editor.js";
import { noteSaved, saveTourOffline } from "./offline.js";

const SORTS = { best: tr("Best"), time: tr("Shortest"), ascent: tr("Least ascent"), altitude: tr("Highest huts"),
  beds: tr("Most free beds"), cost: tr("Cheapest") };
// The search returns a pool of varied tours ordered by "best"; other orders are applied here, without a new search.
const SORT_KEYS = {
  best: (t) => [-t.score], time: (t) => [t.time_min, -t.score],
  ascent: (t) => [t.ascent_m, -t.score], altitude: (t) => [-(t.hut_altitude || 0), -t.score],
  beds: (t) => [-t.metrics.beds, -t.score],
  // Fully priced tours first (partial sums would look cheap), then by the per-person total
  cost: (t) => [t.cost.priced < t.nights ? 1 : 0,
    Object.values(t.cost.totals).reduce((sum, c) => sum + (settings().member ? c.member : c.non_member), 0), -t.score],
};
const PAGE = 10;
const MAX_FLEX = 14; // the API's limit: a search runs once per start date
let results = null;
let order = []; // results.tours in the chosen sort order; state.tour is a 1-based index into it
let current = null; // the open tour: a search result (state.tour) or one opened by id (state.tid)
let booking = false; // the booking checklist is open
let shown = PAGE;
let listed = []; // map lines of the found tours, filtered (renderResults)
let metricLabels = {};
const stopSearch = { timer: null, seq: 0 };
let searchSeq = 0;

/** The editor's tour becomes the open tour (opened by id). */
export function setCurrent(t) {
  current = t;
  setState({ tour: 0, tid: t.id });
}

/** Filters on the found tours (chips in the top bar): they narrow the list without a new search. */
const RESULT_FILTERS = {
  beds: (t) => results?.query?.check_availability && t.availability?.available === t.nights,
  catered: (t) => t.days.every((d) => !d.night || d.night.status === "catered"),
  self: (t) => t.days.every((d) => !d.night || d.night.status === "self_service"),
};
const resultFilters = () => state.rFilter.split(",").filter((f) => RESULT_FILTERS[f]);

function sortResults() {
  const key = SORT_KEYS[state.tSort] || SORT_KEYS.best;
  const cmp = (a, b) => {
    const ka = key(a), kb = key(b);
    for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] - kb[i];
    return 0;
  };
  const filters = resultFilters().map((f) => RESULT_FILTERS[f]);
  order = results ? results.tours.filter((t) => filters.every((f) => f(t))).sort(cmp) : [];
}

function syncResultFilters() {
  const on = resultFilters();
  $("#tourQuick").hidden = !results;
  document.querySelectorAll("[data-rfilter]").forEach((b) => b.setAttribute("aria-pressed", on.includes(b.dataset.rfilter)));
}

function toggleResultFilter(name) {
  let on = resultFilters();
  on = on.includes(name) ? on.filter((f) => f !== name) : [...on.filter((f) => !(EXCLUSIVE[name] === f)), name];
  setResultFilters(on);
}
const EXCLUSIVE = { catered: "self", self: "catered" };  // a tour can't be both

function setResultFilters(on) {
  setState({ rFilter: on.join(",") });
  sortResults();
  shown = PAGE;
  // The open tour keeps its place if it still matches, else back to the list
  if (state.tour) {
    const n = order.indexOf(current) + 1;
    if (n) { setState({ tour: n }); renderTourDetail(current, detailOptions()); } else closeTour();
  }
  renderResults();
}

/** The coming Saturday (today if it is a Saturday), as a local date. */
function defaultTourDate() {
  const d = new Date();
  d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7));
  return localIso(d);
}

// ---------- Form ----------
export function syncTourForm() {
  if (!state.tDate || state.tDate < localIso()) state.tDate = defaultTourDate();
  $("#tDate").min = localIso();
  $("#tDate").value = state.tDate;
  $("#tFlex").value = state.tFlex;
  $("#tNightsLabel").textContent = state.tNights;
  $("#tPeopleLabel").textContent = state.tPeople;
  $("#tNightsWord").textContent = tr("nights", { n: state.tNights });
  $("#tPeopleWord").textContent = tr("people", { n: state.tPeople });
  $("#tMin").value = state.tMin;
  $("#tMax").value = state.tMax;
  $("#tHoursLabel").textContent = `${state.tMin}–${state.tMax} h`;
  $("#tHoursFill").style.left = `${((state.tMin - 1) / 9) * 100}%`;
  $("#tHoursFill").style.right = `${100 - ((state.tMax - 1) / 9) * 100}%`;
  document.querySelectorAll("#tLoop button").forEach((b) => b.setAttribute("aria-pressed", (b.dataset.v === "1") === state.tLoop));
  $("#tLoopField").hidden = !state.tStart;
  $("#tUnknown").checked = state.tUnknown;
  $("#tStartPicked").hidden = !state.tStart;
  $("#tStartInput").hidden = !!state.tStart;
  $("#tStartName").textContent = state.tStartName;
  $("#tAreaField").hidden = !!state.tStart;
  $("#tArea").checked = state.tArea;
}

export function bindTourForm() {
  $("#tDate").addEventListener("change", (e) => {
    setState({ tDate: e.target.value && e.target.value >= localIso() ? e.target.value : defaultTourDate() });
    syncTourForm();
  });
  $("#tFlex").innerHTML = Array.from({ length: MAX_FLEX + 1 }, (_, n) =>
    `<option value="${n}">${n ? tr("up to {n} days later", { n }) : tr("Exact date")}</option>`).join("");
  $("#tFlex").addEventListener("change", (e) => setState({ tFlex: Number(e.target.value) }));
  const stepper = (attr, key, max) => document.querySelectorAll(`[${attr}]`).forEach((b) => b.addEventListener("click", () => {
    setState({ [key]: Math.min(max, Math.max(1, state[key] + Number(b.getAttribute(attr)))) });
    syncTourForm();
  }));
  stepper("data-step", "tNights", 10);
  stepper("data-people", "tPeople", 30);
  $("#tMin").addEventListener("input", (e) => { setState({ tMin: Math.min(Number(e.target.value), state.tMax - 0.5) }); syncTourForm(); });
  $("#tMax").addEventListener("input", (e) => { setState({ tMax: Math.max(Number(e.target.value), state.tMin + 0.5) }); syncTourForm(); });
  for (const [id, key, parse] of [["#tLoop", "tLoop", (v) => v === "1"]]) {
    $(id).addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) { setState({ [key]: parse(b.dataset.v) }); syncTourForm(); }
    });
  }
  $("#tUnknown").addEventListener("change", (e) => setState({ tUnknown: e.target.checked }));
  $("#tArea").addEventListener("change", (e) => setState({ tArea: e.target.checked }));
  bindStopPicker();
  bindYourTours();
  $("#tourQuick").addEventListener("click", (e) => {
    const b = e.target.closest("[data-rfilter]");
    if (b) toggleResultFilter(b.dataset.rfilter);
  });

  $("#tourForm").addEventListener("submit", (e) => {
    e.preventDefault();
    // All of Switzerland, unless limited to the map area (only without a start point: with one, around it).
    const area = state.tArea && !state.tStart;
    setState({ tBox: area ? visibleBox().map((v) => v.toFixed(3)).join(",") : "", tour: 0 });
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
    const step = e.target.closest("[data-step-tour]");
    if (step) { stepTour(Number(step.dataset.stepTour)); return; }
    if (e.target.closest("#tourEdit")) { openEditor(current); return; }
    if (e.target.closest("#tourBook")) {
      booking = !booking;
      renderTourDetail(current, detailOptions());
      if (booking) recheckBeds();
      return;
    }
    if (e.target.closest("#bookRecheck")) { recheckBeds(); return; }
    if (e.target.closest("#tourShare")) { shareTour(current, e.target.closest("#tourShare")); return; }
    if (e.target.closest("#tourKeep")) {
      const entry = findEntry(current.id);
      entry ? removeEntry(entry.key) : keepTour(current);
      renderTourDetail(current, detailOptions());
      if (!entry) saveTourOffline(current);
      return;
    }
    const timetable = e.target.closest("[data-timetable]");
    if (timetable) { toggleTimetable(timetable); return; }
    const hut = e.target.closest("[data-hut]");
    if (hut) { e.preventDefault(); selectHut(Number(hut.dataset.hut)); return; }
    const day = e.target.closest("[data-day]");
    if (day && !e.target.closest("a")) focusDay(Number(day.dataset.day));
  });
  bindTourSwipe();
  $("#tourDetail").addEventListener("change", (e) => {
    const box = e.target.closest("[data-booked]");
    if (!box) return;
    setBooked(box.dataset.booked, box.checked);
    renderTourDetail(current, detailOptions());
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
        `<span class="mode">${modeLabel(s.mode)}</span> ${esc(s.name)}</li>`).join("") || `<li class="none">${tr("No stop found")}</li>`;
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
    start_date: state.tDate, flex_days: Math.min(MAX_FLEX, Math.max(0, state.tFlex)), nights: state.tNights, min_hours: state.tMin, max_hours: state.tMax,
    ...settingsParams(), transit: settings().transit, loop: state.tLoop && !!state.tStart,
    include_unknown: state.tUnknown, start_stop: state.tStart || null,
    people: state.tPeople, check_availability: settings().checkBeds,
    bbox: state.tBox ? state.tBox.split(",").map(Number) : null,
  };
  const seq = ++searchSeq;
  const button = $("#tFind");
  button.disabled = true;
  button.textContent = settings().checkBeds ? tr("Searching and checking free beds…") : tr("Searching…");
  $("#tourResults").innerHTML = "";
  try {
    const res = await fetch("/api/tours/suggest", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (seq !== searchSeq) return;  // a newer search was started meanwhile
    if (res.status === 422) {
      const detail = (await res.json()).detail;
      if (seq !== searchSeq) return;
      $("#tourResults").innerHTML = `<p class="empty">${esc(detail?.[0]?.msg?.replace("Value error, ", "") || tr("Invalid search."))}</p>`;
      return;
    }
    if (!res.ok) throw new Error(`${res.status}`);
    const data = await res.json();
    // Only the criteria that count towards "best" (weight > 0) appear in the score tooltip.
    const labels = Object.fromEntries(((await loadInfo())?.metrics || []).filter((m) => m.weight > 0).map((m) => [m.key, m.label]));
    if (seq !== searchSeq) return;
    results = data;
    metricLabels = labels;
  } catch (err) {
    console.error(err);
    if (seq === searchSeq) $("#tourResults").innerHTML = `<p class="empty">${tr("Search failed. Is the server running?")}</p>`;
    return;
  } finally {
    if (seq === searchSeq) {
      button.disabled = false;
      button.textContent = tr("Find tours");
    }
  }
  sortResults();
  // The open tour by its id (new settings can move it or drop it); a shared link's tour by its position.
  const n = current && state.tour ? order.findIndex((t) => t.id === current.id) + 1 : state.tour;
  shown = Math.max(PAGE, Math.ceil(n / PAGE) * PAGE);
  renderResults();
  if (n && order[n - 1]) selectTour(n);
  else {
    if (state.tour) closeTour();
    fitCoords(listed.flatMap((f) => f.geometry.coordinates.flat()));  // the found tours, on the map
  }
}

/** After a display-only settings change (units, member prices): show the loaded results and tour again. */
export function rerenderTours() {
  sortResults();  // "Cheapest" depends on member prices
  if (state.tour) setState({ tour: order.indexOf(current) + 1 });
  if (results) renderResults();
  if (current) isEditing() ? refreshEditor(current) : renderTourDetail(current, detailOptions());
}

function renderResults() {
  const tours = order;
  syncResultFilters();
  listed = tours.map((t, i) => ({
    type: "Feature", properties: { n: i + 1 },
    geometry: { type: "MultiLineString", coordinates: t.days.map((d) => decodePolyline(d.geometry || "")) },
  }));
  drawListed();
  if (!tours.length && results?.tours.length) {
    $("#tourResults").innerHTML = `<div class="empty"><b>${tr("No tour matches these filters.")}</b><br>
      ${tr("{n} tours were found.", { n: results.tours.length })}<br><button class="link" id="rReset">${tr("Clear the filters")}</button></div>`;
    $("#rReset").addEventListener("click", () => setResultFilters([]));
    return;
  }
  if (!tours.length) {
    $("#tourResults").innerHTML = `<div class="empty"><b>${tr("No tours found.")}</b><br>${state.tStart
      ? tr("Try a wider range of hours, a harder trail, fewer nights, another start, or including huts without opening data.")
      : tr("Try a wider range of hours, a harder trail, fewer nights, a larger map area, or including huts without opening data.")}</div>`;
    return;
  }
  const count = `<strong>${tours.length}</strong>`;
  const heading = tours.length < results.tours.length
    ? tr("{count} of {total} tours", { n: results.tours.length, count, total: results.tours.length })
    : tr("{count} tours", { n: tours.length, count });
  $("#tourResults").innerHTML = `<div class="results-head"><span>${heading}</span>
    <label class="sort">${tr("Sort")} <select id="tSort">${Object.entries(SORTS).map(([v, label]) =>
      `<option value="${v}"${v === state.tSort ? " selected" : ""}>${label}</option>`).join("")}</select></label></div>` +
    tours.slice(0, shown).map(tourCard).join("") +
    (tours.length > shown ? `<button class="btn wide" id="tMore">${tr("Show {n} more", { n: Math.min(PAGE, tours.length - shown) })}</button>` : "");
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
  const bars = t.days.map((d) => `<div class="daybar" title="${tr("Day {day}", { day: d.day })}: ${formatMinutes(d.time_min)} · ${categoryName(d.max_category, d.osm_grade)}">
      <i style="height:${Math.max(18, (d.time_min / longest) * 100)}%;background:${CATEGORY[d.max_category].color}"></i>
      <span>${(d.time_min / 60).toFixed(1).replace(".0", "")}h</span></div>`).join("");
  return `<article class="tour-card" data-n="${i + 1}" tabindex="0">
    <div class="tour-head">
      <div>${results?.query?.flex_days ? `<b>${formatDay(t.days[0].date)}</b> · ` : ""}<b>${tr("{n} nights", { n: t.nights })}</b> · ${distanceValue(t.distance_m, 0)} ${distanceUnit()} · ↑ ${formatHeight(t.ascent_m)}</div>
      <div class="tour-flags">${unofficialBadge(t)}${rideBadge(t)}${transitBadge(t)}${availBadge(t)}${costBadge(t)}${scoreBadge(t)}</div>
    </div>
    <div class="tour-body">
      <ol class="chain">${chainItems(chain)}</ol>
      <div class="daybars${t.days.length > 6 ? " dense" : ""}" style="--days:${t.days.length}">${bars}</div>
    </div>
    ${tourSparkline(t)}
  </article>`;
}


/** "Best" score as a percentage; the tooltip lists the sub-scores (weights come from huthop.toml). */
function scoreBadge(t) {
  if (!t.metrics) return "";
  const parts = Object.entries(metricLabels).map(([k, label]) => `${tr(label)}: ${Math.round(t.metrics[k] * 100)}%`).join("\n");
  return `<span class="score" title="${tr("Best score")}\n${parts}">${Math.round(t.score * 100)}%</span>`;
}

/** Card badge: a day on an unofficial OSM path, with its hardest grade. */
function unofficialBadge(t) {
  const days = t.days.filter((d) => d.max_category === 4);
  if (!days.length) return "";
  const grade = days.map((d) => d.osm_grade).filter(Boolean).sort().pop();
  return `<span class="flag" title="${tr("Not signposted: a path from OpenStreetMap, often glacier or alpine terrain")}">` +
    `${tr("Unofficial path")}${grade ? ` ${grade}` : ""}</span>`;
}

/** Card badge: days with a bus, train or cable car ride between huts. */
function rideBadge(t) {
  const rides = t.days.filter((d) => d.ride);
  if (!rides.length) return "";
  return `<span class="flag" title="${esc(rides.map((d) => `${tr("Day {day}", { day: d.day })}: ${d.ride.line} ${d.ride.from.name} → ${d.ride.to.name}`).join("; "))}">` +
    `${tr("{n} rides between huts", { n: rides.length })}</span>`;
}

/** Card badge when the first connection reaches the first hut late, or the last one leaves early. */
function transitBadge(t) {
  const late = t.access?.late, early = t.exit?.early;
  if (!late && !early) return "";
  const why = [late && tr("first connection: at the hut from {time}", { time: t.access.hut_from }), early && tr("last connection: leave the last hut by {time}", { time: t.exit.leave_by })];
  return `<span class="flag" title="${why.filter(Boolean).join("; ")}">${tr("Tight connection")}</span>`;
}

/** Card badge: estimated hut costs per person ("from" when some nights have no prices). */
function costBadge(t) {
  if (!t.cost?.priced) return "";
  const from = t.cost.priced < t.nights ? `${tr("from (price)")} ` : "";
  return `<span class="flag pending" title="${esc(costTitle(t, settings().member, state.tPeople))}">` +
    `${from}${formatCost(t.cost.totals, settings().member)} ${tr("p.p.")}</span>`;
}

/** Card badge: free beds on every night, or how many nights are not confirmed. */
function availBadge(t) {
  const a = t.availability || {};
  const checked = results?.query?.check_availability;
  if (checked && a.available === t.nights) return `<span class="flag ok" title="${tr("Free beds on every night")}">${tr("Beds free")}</span>`;
  // Beyond the first candidates of a search, free beds are only checked when the tour is opened.
  if (checked && a.not_checked) {
    return `<span class="flag pending" title="${tr("Free beds not checked yet for {n} nights. Open the tour to check them.", { n: a.not_checked })}">${tr("Beds: open to check")}</span>`;
  }
  const unsure = checked ? t.nights - (a.available || 0) : t.uncertain_nights;
  if (!unsure) return "";
  const title = checked ? tr("Nights without confirmed free beds (not bookable yet or no online booking)")
    : tr("Opening data missing or only part of the month");
  return `<span class="flag" title="${title}">${tr("{n} nights unconfirmed", { n: unsure })}</span>`;
}

/** Start, huts and end of a tour card; long tours show the first two huts, "+ N more huts" and the last hut. */
function chainItems(chain) {
  const item = (p, end) => `<li class="${p.kind}">${end ? `<span class="mode">${modeLabel(p.mode)}</span> ` : ""}${esc(p.name)}</li>`;
  const huts = chain.slice(1, -1);
  const shown = huts.length > 4
    ? [...huts.slice(0, 2).map((p) => item(p)), `<li class="more">${tr("+ {n} more huts", { n: huts.length - 3 })}</li>`, item(huts[huts.length - 1])]
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
        <span>${esc(e.date)} · ${tr("{n} nights", { n: e.nights })} · ${esc(e.huts.join(", "))}</span>
      </button>
      <button class="conn-x" data-remove="${esc(e.key)}" title="${tr("Remove from my tours")}" aria-label="${tr("Remove")}">×</button>
    </li>`).join("") : `<li class="hint">${tr("No tours yet. Open a tour and choose “Add to my tours”, edit one, or plan one from a hut's details.")}</li>`;
}

// ---------- Tour detail ----------
const detailOptions = () => ({ kept: !!(current && findEntry(current.id)), booking, people: bedsPeople(),
  nav: state.tour && results ? { n: state.tour, total: order.length } : null });
/** The group size free beds are checked for: the search's, else the form's. */
const bedsPeople = () => (state.tour && results ? results.query.people : state.tPeople);

function selectTour(n) {
  const t = order[n - 1];
  if (!t) return;
  current = t;
  setState({ tour: n, tid: "", tEdit: false });
  showTour(t);
}

/** The next (+1) or previous (-1) search result, from an open one. */
function stepTour(dir) {
  const n = state.tour + dir;
  if (!state.tour || isEditing() || !order[n - 1]) return;
  if (n > shown) { shown = Math.ceil(n / PAGE) * PAGE; renderResults(); }  // its card is there when you go back
  selectTour(n);
}

/** Phones: swipe left or right across an open search result for the next or previous one. */
function bindTourSwipe() {
  let start = null;
  $("#tourDetail").addEventListener("touchstart", (e) => {
    start = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null;
  }, { passive: true });
  $("#tourDetail").addEventListener("touchend", (e) => {
    if (!start) return;
    const dx = e.changedTouches[0].clientX - start.x, dy = e.changedTouches[0].clientY - start.y;
    start = null;
    if (Math.abs(dx) > 60 && Math.abs(dx) > 2 * Math.abs(dy)) stepTour(dx < 0 ? 1 : -1);  // mostly sideways
  });
}

/** Share a tour's preview link (/t/{id}, see app/api/tours.py): the phone's share sheet, else copy it. */
async function shareTour(t, button) {
  if (!await shareLink(`${location.origin}/t/${encodeURIComponent(t.id)}`, tr("HutHop tour"))) return;
  button.textContent = `✓ ${tr("Link copied")}`;
  setTimeout(() => { button.textContent = tr("Share"); }, 2000);
}

/** A tour by id (my tours, a shared link, the editor's result), or null if it no longer exists. */
export async function openTourById(id, { edit = false } = {}) {
  const res = await fetch(`/api/tours/${encodeURIComponent(id)}?people=${state.tPeople}&${settingsQuery()}`);
  $("#tourGone")?.remove();
  if (!res.ok) {
    // in the tab you are looking at, replacing an earlier warning
    $(state.view === "mine" ? "#mineTab" : "#tourResults").insertAdjacentHTML("afterbegin", `<p class="warn" id="tourGone">
      ${tr("This tour is no longer available (a connection was removed or excluded).")}</p>`);
    return null;
  }
  noteSaved(res);
  current = await res.json();
  setState({ tour: 0, tid: id, tEdit: edit });
  if (edit) openEditor(current);
  else showTour(current);
  return current;
}

/** Show a tour in the detail view (also used by the editor for its result). */
export function showTour(t) {
  if (t !== current) booking = false;
  current = t;
  renderTourDetail(t, detailOptions());
  $("#browse").hidden = true;
  $("#detail").hidden = true;
  $("#tourDetail").hidden = false;
  $("#tourDetail").scrollTop = 0;
  clearConnections();
  drawTour(t);
  drawListed();
  revealPanel();
  checkTourBeds(t);
  if (findEntry(t.id)) saveTourOffline(t);
}

/** Free beds of a tour's nights not checked yet (beyond a search's first candidates, or edited / opened by id):
 * checked when the tour is shown, if the search asks for free beds. */
export async function checkTourBeds(t, { fresh = false } = {}) {
  const fromSearch = !!(state.tour && results);
  const check = fromSearch ? results.query.check_availability : settings().checkBeds;
  const unchecked = t.days.some((d) => d.night && (!d.night.availability || d.night.availability.state === "not_checked"));
  if (!fresh && !(check && unchecked)) return;
  const res = await fetch(`/api/tours/${encodeURIComponent(t.id)}?people=${bedsPeople()}&check_availability=true` +
    `${fresh ? "&fresh=true" : ""}&${settingsQuery()}`);
  if (!res.ok) return;
  const checked = await res.json();
  // Keep score and order (computed for the search's hours); take only the nights' availability.
  t.days.forEach((d, i) => { d.night = checked.days[i].night; });
  t.availability = checked.availability;
  if (fromSearch) renderResults();  // the card's badge, for when the list is shown again
  if (current === t && !$("#tourDetail").hidden) {
    if (isEditing()) refreshEditor(t);
    else renderTourDetail(t, detailOptions());
  }
}

/** Right before booking: free beds of every night, from calendars at most a minute old. */
async function recheckBeds() {
  const button = $("#bookRecheck");
  if (button) { button.disabled = true; button.textContent = tr("Checking free beds…"); }
  await checkTourBeds(current, { fresh: true });
  if (button?.isConnected) { button.disabled = false; button.textContent = tr("Check free beds again"); }
}

export function closeTour() {
  closeEditor();
  setState({ tour: 0, tid: "", tEdit: false });
  current = null;
  clearTour();
  $("#tourDetail").hidden = true;
  $("#browse").hidden = false;
  drawListed();
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
    geometry: { type: "MultiLineString", coordinates: (d.walks || [d]).map((w) => decodePolyline(w.geometry || "")) },
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
  if (fit) fitCoords(t.days.flatMap(dayCoords));
}

function focusDay(day) {
  const t = current;
  if (t) fitCoords(dayCoords(t.days[day - 1]), 14);
}

/** A day's route points; a day with a ride has two walks. */
const dayCoords = (d) => (d.walks || [d]).flatMap((w) => decodePolyline(w.geometry || ""));

/** Remove a hovered preview; keeps the selected tour drawn while its detail is open. */
export function clearTour() {
  map?.getSource("tour")?.setData(EMPTY);
  map?.getSource("tour-points")?.setData(EMPTY);
  if (!$("#tourDetail").hidden) drawTour(current, { fit: false });
  drawListed();
}

export function redrawTour() {
  if (current && !$("#tourDetail").hidden) drawTour(current, { fit: false });
  drawListed();
}

/** The found tours as thin lines, only while the list itself is shown (not over an open tour). */
function drawListed() {
  const show = state.view === "tours" && $("#tourDetail").hidden && $("#detail").hidden && !$("#browse").hidden;
  map?.getSource("tours-listed")?.setData(show ? { type: "FeatureCollection", features: listed } : EMPTY);
}

/** A found tour's line on the map: hover shows the tour and marks its card, a click opens it (a hut under the
 * pointer wins). Events go to a wide invisible line, so the thin one is easy to hit. */
export function bindTourMap() {
  let hovered = 0;
  const mark = (n) => {
    document.querySelectorAll(".tour-card.hover").forEach((el) => el.classList.remove("hover"));
    document.querySelector(`.tour-card[data-n="${n}"]`)?.classList.add("hover");
  };
  map.on("mousemove", "tours-hit", (e) => {
    const n = e.features[0].properties.n;
    map.getCanvas().style.cursor = "pointer";
    if (n === hovered || !$("#tourDetail").hidden) return;
    hovered = n;
    drawTour(order[n - 1], { fit: false });
    mark(n);
  });
  map.on("mouseleave", "tours-hit", () => {
    map.getCanvas().style.cursor = "";
    hovered = 0;
    mark(0);
    clearTour();
  });
  map.on("click", "tours-hit", (e) => {
    if (map.queryRenderedFeatures(e.point, { layers: ["huts"] }).length) return;
    const n = e.features[0].properties.n;
    if (n > shown) { shown = Math.ceil(n / PAGE) * PAGE; renderResults(); }  // its card is there when you go back
    hovered = 0;
    selectTour(n);
  });
}

export function hasResults() {
  return !!results;
}

export function addTourLayers() {
  map.addSource("tours-listed", { type: "geojson", data: EMPTY });
  map.addLayer({  // under the hut dots, so huts stay clickable
    id: "tours-listed", type: "line", source: "tours-listed", layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#1d2521", "line-opacity": 0.6, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 2.5, 13, 4] },
  }, map.getLayer("huts-selected") ? "huts-selected" : undefined);
  map.addLayer({ id: "tours-hit", type: "line", source: "tours-listed", paint: { "line-width": 16, "line-opacity": 0 } },
    map.getLayer("huts-selected") ? "huts-selected" : undefined);
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

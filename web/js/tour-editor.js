// Tour editor: change any tour by hand. The server lists every single edit of a tour with the tour it gives
// (GET /api/tours/{id}/options, planner/editing.py): another start or end stop, another hut for a night, a night
// added or removed, another trail variant for a day; the start date is edited here. Edit mode replaces the tour
// view: each slot opens its candidates (huts and stops nearest first) in the panel and on the map, and a click on
// a hut on the map offers the edits it fits. Undo and redo; the result is kept in "My tours" as a new entry (the
// tour it started from stays as it was). A tour you design needn't follow the search settings (hours, trail
// level), so nothing is refused or flagged for them.
import { $, CATEGORY, EMPTY, esc, formatMinutes, localIso, revealPanel } from "./util.js";
import { setState, state } from "./state.js";
import { fitCoords, map } from "./map.js";
import { closeDetail } from "./huts.js";
import { AVAIL_LABEL, NIGHT_LABEL, connectionLine, formatDay, outsideSettings, stopLabel } from "./tour-detail.js";
import { distanceUnit, distanceValue, formatDistance, formatHeight, heightUnit, heightValue, settingsParams,
  settingsQuery } from "./settings.js";
import { checkTourBeds, drawTour, openTourById, setCurrent, showTour } from "./tours.js";
import { keepTour, updateEntry } from "./your-tours.js";

const SHOWN = 8; // candidates listed before "Show all"
let ed = null; // { tour, options, history: [tour ids], pos, entryKey, slot, showAll, pick, token }

export const isEditing = () => !!ed;

/** Start editing a tour (it stays as it was; the first change creates a new entry in My tours). */
export async function openEditor(t) {
  ed = { tour: t, options: [], history: [t.id], pos: 0, entryKey: null, slot: null, showAll: false, pick: null };
  setState({ tEdit: true });
  $("#browse").hidden = true;
  $("#detail").hidden = true;
  $("#tourDetail").hidden = false;
  render({ loading: true });
  revealPanel();
  await load(t.id, t);
}

/** After the settings changed: the same tour, with the edits the new settings allow (and times at the new pace). */
export function reloadEditor() {
  if (ed) load(ed.tour.id, ed.tour);
}

/** After a free-beds check of the edited tour: show the nights' new availability. */
export function refreshEditor(t) {
  if (!ed || ed.tour !== t) return;
  render();
}

/** Leave the editor without showing the tour again (the tour is being closed). */
export function closeEditor() {
  if (!ed) return;
  ed = null;
  clearCandidates();
}

function done() {
  const t = ed.tour;
  ed = null;
  setState({ tEdit: false });
  clearCandidates();
  showTour(t);
}

/** Load a tour and its edits; `known` keeps the nights' availability of an already loaded version. */
async function load(tourId, known = null) {
  const token = ed.token = {};  // only the latest load (and editor) may show its answer
  const res = await fetch(`/api/tours/${encodeURIComponent(tourId)}/options?${settingsQuery()}`);
  if (ed?.token !== token) return false;
  if (!res.ok) {
    $("#edStatus")?.replaceChildren(document.createTextNode("Could not load the edits for this tour."));
    return false;
  }
  const data = await res.json();
  if (ed?.token !== token) return false;
  if (known && known.id === data.tour.id) {
    data.tour.days.forEach((d, i) => { if (d.night && known.days[i]?.night) d.night = known.days[i].night; });
    data.tour.availability = known.availability;
  }
  ed.tour = data.tour;
  ed.options = data.options;
  ed.slot = null;
  ed.pick = null;
  setCurrent(ed.tour);
  setState({ tEdit: true });
  render();
  drawTour(ed.tour, { fit: false });
  clearCandidates();
  checkTourBeds(ed.tour);
  return true;
}

/** Switch to another version of the tour (an edit, or undo / redo with `record` false). */
async function apply(tourId, { record = true } = {}) {
  if (!ed || tourId === ed.tour.id) return;
  if (record) {
    ed.history = [...ed.history.slice(0, ed.pos + 1), tourId];
    ed.pos = ed.history.length - 1;
  }
  render({ loading: true });
  if (!(await load(tourId))) return;
  // The edited tour is a new entry in My tours; further edits update that entry.
  if (ed.entryKey) updateEntry(ed.entryKey, ed.tour);
  else if (ed.pos > 0) ed.entryKey = keepTour(ed.tour).key;
}

function step(delta) {
  const pos = ed.pos + delta;
  if (pos < 0 || pos >= ed.history.length) return;
  ed.pos = pos;
  apply(ed.history[pos], { record: false });
}

// ---------- Rendering ----------
const options = (kind, slot = null) => ed.options.filter((o) => o.kind === kind && (slot === null || o.slot === slot));

function candidateList(list) {
  if (!list.length) return `<p class="hint">Nothing connects here.</p>`;
  const shown = ed.showAll ? list : list.slice(0, SHOWN);
  const items = shown.map((o) => {
    const name = o.place ? (o.place.kind === "stop" ? stopLabel(o.place) : esc(o.place.name)) : "Without this night";
    const extra = o.service ? serviceSummary(o) : o.night ? `<span class="night ${o.night.status}">${NIGHT_LABEL[o.night.status]}</span>` : "";
    return `<li><button class="cand" data-apply="${esc(o.tour_id)}"${o.place ? ` data-place="${o.place.id}"` : ""}>
        <span class="cand-name">${name}</span>
        <span class="cand-meta">${o.distance_m !== undefined ? `<b>${formatDistance(o.distance_m)} · ${formatMinutes(o.walk_min)}</b> away · ` : ""}tour
          ${formatMinutes(o.time_min)} (${o.days.map((m) => (m / 60).toFixed(1).replace(".0", "")).join(" / ")} h) ·
          ↑ ${formatHeight(o.ascent_m)}${extra ? ` · ${extra}` : ""}</span>
      </button></li>`;
  }).join("");
  const more = !ed.showAll && list.length > SHOWN ? `<button class="link inline" id="edAll">Show all ${list.length}</button>` : "";
  return `<ul class="cands">${items}</ul>${more}`;
}

function serviceSummary(o) {
  const s = o.service;
  if (!s.known) return "no timetable yet";
  if (!s.calls) return `<span class="warn-text">no service that day</span>`;
  return o.kind === "start" ? `first ${s.first}${s.late ? " (late)" : ""}` : `last ${s.last}${s.early ? " (early)" : ""}`;
}

/** A slot: its button and, while it is open, its candidates. */
function slot(key, label, list) {
  if (!list.length) return "";
  const open = ed.slot === key;
  return `<div class="slot${open ? " open" : ""}">
      <button class="link inline" data-slot="${key}" aria-expanded="${open}">${label} <span class="count">${list.length}</span></button>
      ${open ? candidateList(list) : ""}
    </div>`;
}

function limitChips(d) {
  const list = options("limit", d);
  if (!list.length) return "";
  return `<div class="chips">${list.map((o) => `<button class="chip small" data-apply="${esc(o.tour_id)}">
      ${CATEGORY[o.max_category].name}: ${formatMinutes(o.days[d])}</button>`).join("")}</div>`;
}

function render({ loading = false } = {}) {
  if (!ed) return;
  const t = ed.tour;
  const n = t.nights;
  const start = t.days[0].from, end = t.days[n].to;
  const rows = [];
  rows.push(`<li class="tl-stop"><div class="tl-dot"></div><div><b>Start</b> ${stopLabel(start)}
      ${connectionLine(t.access, "access")}${slot("start", "Other start stop", options("start"))}</div></li>`);
  rows.push(insertRow(0));
  t.days.forEach((d, i) => {
    const cat = CATEGORY[d.max_category];
    rows.push(`<li class="tl-day" data-day="${d.day}"><div class="tl-dot" style="--c:${cat.color}"></div>
        <div><div class="tl-title">Day ${d.day} · ${formatDay(d.date)}</div>
          <div class="tl-meta"><b>${formatMinutes(d.time_min)}</b> · ${formatDistance(d.distance_m)} · ↑ ${formatHeight(d.ascent_m)} ·
            ↓ ${formatHeight(d.descent_m)} · <span style="color:${cat.color}">${cat.name}</span></div>
          ${limitChips(i)}</div></li>`);
    if (!d.night) return;
    const remove = options("remove", i)[0];
    const a = d.night.availability;
    rows.push(`<li class="tl-night"><div class="tl-dot hut"><span>${d.day}</span></div>
        <div><b>${esc(d.to.name)}</b>
          <div class="tl-meta">Night of ${formatDay(d.date)} · <span class="night ${d.night.status}">${NIGHT_LABEL[d.night.status]}</span>${
            a && a.state !== "not_checked" ? ` · ${AVAIL_LABEL[a.state](a)}` : ""}</div>
          <div class="slot-row">${slot(`swap:${i}`, "Other hut", options("swap", i))}
            ${remove ? `<button class="link inline danger" data-apply="${esc(remove.tour_id)}">Remove this night</button>` : ""}</div>
        </div></li>`);
    rows.push(insertRow(i + 1));
  });
  rows.push(`<li class="tl-stop"><div class="tl-dot"></div><div><b>End</b> ${stopLabel(end)}
      ${connectionLine(t.exit, "exit")}${slot("end", "Other end stop", options("end"))}</div></li>`);

  $("#tourDetail").innerHTML = `
    <div class="tour-top editor-top">
      <div class="editor-bar">
        <button class="btn primary" id="edDone">Done</button>
        <button class="btn" id="edUndo" ${ed.pos === 0 ? "disabled" : ""} title="Undo">↶ Undo</button>
        <button class="btn" id="edRedo" ${ed.pos >= ed.history.length - 1 ? "disabled" : ""} title="Redo">↷ Redo</button>
        <span class="editing-label">Editing</span>
      </div>
      <h2>${esc(start.name)} → ${esc(end.name)}</h2>
      <div class="subtitle">${n} night${n > 1 ? "s" : ""} · ${formatDay(t.days[0].date)} – ${formatDay(t.days[n].date)}</div>
      <div class="field editor-date"><label class="field-label" for="edDate">Start date</label>
        <input class="input" type="date" id="edDate" value="${t.days[0].date}" min="${localIso()}"></div>
      <div class="facts">
        <div class="fact"><b>${formatMinutes(t.time_min).replace(" h", "")}</b><span>hours walking</span></div>
        <div class="fact"><b>${distanceValue(t.distance_m)}</b><span>${distanceUnit()}</span></div>
        <div class="fact"><b>${heightValue(t.ascent_m)}</b><span>${heightUnit()} ascent</span></div>
      </div>
      ${outsideSettings(t)}
      <p class="hint" id="edStatus">${loading ? "Updating…" : "Choose a part of the tour to change it, or click a hut on the map."}</p>
      ${ed.pick ? pickMenu() : ""}
    </div>
    <div class="detail-body"><ol class="timeline editor">${rows.join("")}</ol></div>`;
}

function insertRow(k) {
  const n = ed.tour.nights;
  const label = k === 0 ? "Add a night before" : k === n ? "Add a night after" : "Add a night here";
  const html = slot(`insert:${k}`, `+ ${label}`, options("insert", k));
  return html ? `<li class="tl-insert"><div class="tl-dot add"></div><div>${html}</div></li>` : "";
}

/** The edits a hut clicked on the map fits. */
function pickMenu() {
  const { name, list } = ed.pick;
  if (!list.length) {
    return `<div class="pick"><b>${esc(name)}</b><p class="hint">This hut doesn't connect to the tour where a night could
      change or be added.</p><button class="link inline" id="edPickClose">Close</button></div>`;
  }
  const what = (o) => o.kind === "swap" ? `Instead of night ${o.slot + 1}` :
    o.slot === 0 ? "As a night before the first" : o.slot === ed.tour.nights ? "As a night after the last" : `As a night between nights ${o.slot} and ${o.slot + 1}`;
  return `<div class="pick"><b>${esc(name)}</b><ul class="cands">${list.map((o) => `<li><button class="cand" data-apply="${esc(o.tour_id)}">
      <span class="cand-name">${what(o)}</span><span class="cand-meta">${formatMinutes(o.time_min)} walking</span></button></li>`).join("")}</ul>
    <button class="link inline" id="edPickClose">Close</button></div>`;
}

// ---------- Map ----------
/** Candidates of the open slot (huts or stops) as dots on the map; a click applies one. */
function drawCandidates() {
  const source = map?.getSource("edit-cands");
  if (!source) return;
  if (!ed?.slot) { source.setData(EMPTY); return; }
  const [kind, slotNo] = ed.slot.split(":");
  const list = options(kind, slotNo === undefined ? null : Number(slotNo)).filter((o) => o.place);
  source.setData({ type: "FeatureCollection", features: list.map((o) => ({
    type: "Feature", geometry: { type: "Point", coordinates: [o.place.lon, o.place.lat] },
    properties: { tour_id: o.tour_id, label: o.place.name, best: o === list[0] },
  })) });
}

/** Show the open slot's candidates and the tour's places on the map. */
function fitCandidates() {
  const [kind, slotNo] = ed.slot.split(":");
  const places = options(kind, slotNo === undefined ? null : Number(slotNo)).filter((o) => o.place).map((o) => o.place);
  const tour = [ed.tour.days[0].from, ...ed.tour.days.map((d) => d.to)];
  if (places.length) fitCoords([...places, ...tour].map((p) => [p.lon, p.lat]));
}

function clearCandidates() {
  map?.getSource("edit-cands")?.setData(EMPTY);
}

export function addEditorLayers() {
  map.addSource("edit-cands", { type: "geojson", data: EMPTY });
  map.addLayer({
    id: "edit-cands", type: "circle", source: "edit-cands",
    paint: { "circle-radius": 8, "circle-color": ["case", ["get", "best"], "#c8102e", "#ffffff"],
      "circle-stroke-color": "#c8102e", "circle-stroke-width": 2.5 },
  });
  map.addLayer({
    id: "edit-cands-labels", type: "symbol", source: "edit-cands",
    layout: { "text-field": ["get", "label"], "text-font": ["Frutiger Neue Condensed Bold"], "text-size": 12,
      "text-variable-anchor": ["left", "right", "top", "bottom"], "text-radial-offset": 1.1, "text-optional": true },
    paint: { "text-color": "#c8102e", "text-halo-color": "#ffffff", "text-halo-width": 1.8 },
  });
  drawCandidates();
}

/** A hut clicked on the map while editing: the edits it fits (handled here instead of opening the hut). */
export function editorHutClick(hutId, name) {
  if (!ed) return false;
  const list = ed.options.filter((o) => (o.kind === "swap" || o.kind === "insert") && o.place?.id === hutId);
  ed.pick = { name, list };
  ed.slot = null;
  render();
  clearCandidates();
  $("#tourDetail").scrollTop = 0;
  return true;
}

// ---------- Events ----------
export function bindEditorEvents() {
  $("#tourDetail").addEventListener("click", (e) => {
    if (!ed) return;
    if (e.target.closest("#edDone")) { done(); return; }
    if (e.target.closest("#edUndo")) { step(-1); return; }
    if (e.target.closest("#edRedo")) { step(1); return; }
    if (e.target.closest("#edAll")) { ed.showAll = true; render(); return; }
    if (e.target.closest("#edPickClose")) { ed.pick = null; render(); return; }
    const applyButton = e.target.closest("[data-apply]");
    if (applyButton) { apply(applyButton.dataset.apply); return; }
    const slotButton = e.target.closest("[data-slot]");
    if (slotButton) {
      ed.slot = ed.slot === slotButton.dataset.slot ? null : slotButton.dataset.slot;
      ed.showAll = false;
      ed.pick = null;
      render();
      drawCandidates();
      if (ed.slot) fitCandidates();
    }
  });
  $("#tourDetail").addEventListener("change", (e) => {
    if (!ed || e.target.id !== "edDate" || !e.target.value) return;
    apply(`${e.target.value}${ed.tour.id.slice(10)}`);  // tour ids start with the date (YYYY-MM-DD)
  });
  map.on("click", "edit-cands", (e) => {
    if (ed) apply(e.features[0].properties.tour_id);
  });
  map.on("mouseenter", "edit-cands", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "edit-cands", () => { map.getCanvas().style.cursor = ""; });
  // "Plan a tour from here" in a hut's details
  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-plan-tour]");
    if (button) planFromHut(Number(button.dataset.planTour), button);
  });
}

/** A one-night tour at a hut (the planner's date and hours), opened in the editor and kept in My tours. */
async function planFromHut(hutId, button) {
  button.disabled = true;
  const params = new URLSearchParams({ start_date: state.tDate, min_hours: state.tMin, max_hours: state.tMax,
    ...settingsParams() });
  const res = await fetch(`/api/huts/${hutId}/tour?${params}`);
  button.disabled = false;
  if (!res.ok) {
    button.insertAdjacentHTML("afterend", `<p class="warn">${res.status === 404 ? "No public transport within walking distance of this hut." : "Could not plan a tour."}</p>`);
    return;
  }
  const { tour_id: tourId } = await res.json();
  closeDetail();
  document.querySelector('#tabs [data-view="mine"]')?.click();
  const t = await openTourById(tourId, { edit: true });
  if (t && ed) ed.entryKey = keepTour(t).key;
}

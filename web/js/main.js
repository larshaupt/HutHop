// Entry point: wires the modules together, global controls and start-up.
import { $, isMobile, setSheet } from "./util.js";
import { reloadFromHash, setState, state } from "./state.js";
import { addTrailLayer, initMap, setBaseStyle, setTrailsVisible } from "./map.js";
import { addHutLayers, bindHutEvents, bindHutMap, closeDetail, loadHuts, renderControls, selectHut, setHutClickHook, syncControls,
  update } from "./huts.js";
import { addEdgeLayers, bindConnectionEvents, redrawConnections } from "./connections.js";
import { bindEditorEvents } from "./editor.js";
import { bindAbout } from "./info.js";
import { addTourLayers, bindTourForm, clearTour, closeTour, hasResults, openTourById, redrawTour, runTourSearch, syncTourForm }
  from "./tours.js";
import { addEditorLayers, bindEditorEvents as bindTourEditorEvents, editorHutClick, isEditing, reloadEditor }
  from "./tour-editor.js";
import { bindSettings, onSettingsChange } from "./settings.js";

/** Our layers, bottom to top. Runs after every base-style (re)load. */
function addLayers() {
  addTrailLayer();
  addEdgeLayers();
  addHutLayers();
  addTourLayers();
  addEditorLayers();
  redrawConnections();
  redrawTour();
}

function setView(view) {
  setState({ view });
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === view));
  $("#hutsTab").hidden = view !== "huts";
  $("#toursTab").hidden = view !== "tours";
  $("#mineTab").hidden = view !== "mine";
  if (view === "huts" && state.tour) setState({ tour: 0 });
  if (view !== "tours" || (hasResults() && !state.tour)) clearTour();
}

/** After a change in the settings (⚙): show everything open again within them (units, pace, what is offered). */
let settingsTimer = null;
function applySettings() {
  clearTimeout(settingsTimer);
  settingsTimer = setTimeout(() => {
    update();
    syncControls();
    if (state.hut) {
      selectHut(state.hut, { fly: false });
      if (hasResults() && !state.tour && !state.tid) runTourSearch();  // the list behind it, without opening a tour
    } else if (isEditing()) reloadEditor();
    else if (state.tid) openTourById(state.tid);
    else if (hasResults()) runTourSearch();
  }, 300);
}

function bindGlobalEvents() {
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) setView(b.dataset.view);
  });
  $("#base").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.dataset.v === state.base) return;
    setState({ base: b.dataset.v });
    syncControls();
    setBaseStyle(state.base);
  });
  $("#trails").addEventListener("click", () => {
    setState({ trails: !state.trails });
    syncControls();
    setTrailsVisible(state.trails);
  });
  $("#sheetHandle").addEventListener("click", () => {
    if (sheetDragged) return;  // the end of a drag, not a tap
    setSheet({ peek: "half", half: "full", full: "peek" }[$("#panel").dataset.sheet]);
  });
  bindSheetDrag();
  $("#q").addEventListener("focus", () => { if (isMobile()) setSheet("full"); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !e.target.closest("input, textarea")) {
      e.preventDefault();
      if (state.hut) closeDetail();
      setView("huts");
      $("#q").focus();
    } else if (e.key === "Escape") {
      if (state.hut) closeDetail();
      else if (state.tour || state.tid) closeTour();
      else if (document.activeElement === $("#q")) $("#q").blur();
    }
  });
  window.addEventListener("hashchange", () => {
    const previousHut = state.hut;
    reloadFromHash();
    update();
    if (state.hut !== previousHut) state.hut ? selectHut(state.hut) : closeDetail();
  });
}

const SHEETS = ["full", "half", "peek"];  // top to bottom
const DRAG_ZONES = "#sheetHandle, #browse .brand, #tabs";
let sheetDragged = false;

/** Mobile: drag the bottom sheet by its handle, header or tabs. It follows the finger and settles at the nearest
 * position (full, half, peek), or the next one in the direction of a quick flick. A tap on the handle still cycles. */
function bindSheetDrag() {
  const panel = $("#panel");
  let drag = null;
  // translateY of each position, as in style.css: the panel is 92dvh high, half shows 55dvh, peek shows --peek
  const offsets = () => {
    const h = panel.offsetHeight, peek = parseFloat(getComputedStyle(panel).getPropertyValue("--peek")) || 148;
    return { full: 0, half: h - (h / 0.92) * 0.55, peek: h - peek };
  };
  panel.addEventListener("pointerdown", (e) => {
    if (!isMobile() || !e.isPrimary || !e.target.closest(DRAG_ZONES)) return;
    const y = new DOMMatrix(getComputedStyle(panel).transform).m42;
    drag = { id: e.pointerId, startY: e.clientY, y, from: y, moved: false, at: e.timeStamp, v: 0 };
    sheetDragged = false;
  });
  panel.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dy = e.clientY - drag.startY;
    if (!drag.moved) {
      if (Math.abs(dy) < 6) return;
      drag.moved = true;
      panel.setPointerCapture(e.pointerId);
      panel.style.transition = "none";
      document.activeElement?.blur();  // e.g. the hut search, so the keyboard doesn't fight the drag
    }
    const y = Math.min(Math.max(drag.from + dy, 0), offsets().peek);
    drag.v = (y - drag.y) / Math.max(1, e.timeStamp - drag.at);  // px per ms, downwards positive
    drag.y = y;
    drag.at = e.timeStamp;
    panel.style.transform = `translateY(${y}px)`;
  });
  const end = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const { moved, y, v } = drag;
    drag = null;
    if (!moved) return;
    sheetDragged = true;
    setTimeout(() => { sheetDragged = false; });  // swallow only the click that ends this drag
    const pos = offsets();
    let target = SHEETS.reduce((a, b) => (Math.abs(pos[b] - y) < Math.abs(pos[a] - y) ? b : a));
    if (Math.abs(v) > 0.5) {  // a flick: the next position in its direction
      const next = v > 0 ? SHEETS.find((m) => pos[m] > y + 1) : [...SHEETS].reverse().find((m) => pos[m] < y - 1);
      target = next || target;
    }
    panel.style.transition = "";
    panel.style.transform = "";
    setSheet(target);
  };
  panel.addEventListener("pointerup", end);
  panel.addEventListener("pointercancel", end);
}

async function main() {
  renderControls();
  bindHutEvents();
  bindConnectionEvents();
  bindTourForm();
  bindEditorEvents();
  bindAbout();
  bindSettings();
  onSettingsChange(applySettings);
  bindGlobalEvents();
  initMap(addLayers);
  bindHutMap();
  bindTourEditorEvents();
  setHutClickHook(editorHutClick);
  await loadHuts();
  update();
  syncTourForm();
  setView(state.view);
  if (state.view === "tours" && state.tour) await runTourSearch();
  else if ((state.view === "tours" || state.view === "mine") && state.tid) await openTourById(state.tid, { edit: state.tEdit });
  if (state.hut) selectHut(state.hut);
}

main().catch((err) => {
  console.error(err);
  $("#list").innerHTML = `<li class="empty">Could not load huts. Is the server running?</li>`;
});

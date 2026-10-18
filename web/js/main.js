// Entry point: wires the modules together, global controls and start-up.
import { $, isMobile, setSheet } from "./util.js";
import { reloadFromHash, setState, state } from "./state.js";
import { addTrailLayer, initMap, setBaseStyle, setTrailsVisible } from "./map.js";
import { addHutLayers, bindHutEvents, bindHutMap, closeDetail, loadHuts, renderControls, selectHut, syncControls, update } from "./huts.js";
import { addEdgeLayers, bindConnectionEvents, redrawConnections } from "./connections.js";
import { bindEditorEvents } from "./editor.js";
import { addTourLayers, bindTourForm, clearTour, closeTour, hasResults, redrawTour, runTourSearch, syncTourForm } from "./tours.js";

/** Our layers, bottom to top. Runs after every base-style (re)load. */
function addLayers() {
  addTrailLayer();
  addEdgeLayers();
  addHutLayers();
  addTourLayers();
  redrawConnections();
  redrawTour();
}

function setView(view) {
  setState({ view });
  document.querySelectorAll("#tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === view));
  $("#hutsTab").hidden = view !== "huts";
  $("#toursTab").hidden = view !== "tours";
  if (view === "huts" && state.tour) setState({ tour: 0 });
  if (view === "huts" || (hasResults() && !state.tour)) clearTour();
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
    setSheet({ peek: "half", half: "full", full: "peek" }[$("#panel").dataset.sheet]);
  });
  $("#q").addEventListener("focus", () => { if (isMobile()) setSheet("full"); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !e.target.closest("input, textarea")) {
      e.preventDefault();
      if (state.hut) closeDetail();
      setView("huts");
      $("#q").focus();
    } else if (e.key === "Escape") {
      if (state.hut) closeDetail();
      else if (state.tour) closeTour();
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

async function main() {
  renderControls();
  bindHutEvents();
  bindConnectionEvents();
  bindTourForm();
  bindEditorEvents();
  bindGlobalEvents();
  initMap(addLayers);
  bindHutMap();
  await loadHuts();
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

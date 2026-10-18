// Manual corrections of hut data (overrides that survive SAC re-syncs) and huts added by hand.
import { $, MONTHS, esc, revealPanel } from "./util.js";
import { map } from "./map.js";
import { closeDetail, invalidateHut, reloadHuts, selectHut } from "./huts.js";
import { errorText } from "./connections.js";

const FIELD_LABELS = { name: "Name", altitude: "Altitude (m)", beds: "Beds", shelter_beds: "Winter room places",
  website: "Website", hrs_id: "hut-reservation.org id" };
const NUMERIC = new Set(["altitude", "beds", "shelter_beds", "hrs_id"]);
const CODE_LABEL = { 0: "✓", 1: "½", 2: "–" };
const CODE_TITLE = { 0: "whole month", 1: "part of the month", 2: "no" };
let editing = null; // { hutId, data } while the edit form is open

// ---------- Season editor (shared by edit and add) ----------
function seasonEditor(months) {
  const row = (kind, label) => `<div class="season-edit-row"><span>${label}</span>${months[kind].map((m, i) =>
    `<button type="button" class="code c${m.value}${m.overridden ? " edited" : ""}" data-kind="${kind}" data-month="${i + 1}"
      data-value="${m.value}" title="${MONTHS[i]}: ${CODE_TITLE[m.value]}${m.overridden ? ` (SAC: ${CODE_TITLE[m.source]})` : ""}">
      ${CODE_LABEL[m.value]}</button>`).join("")}</div>`;
  return `<div class="season-edit">
      <div class="season-edit-row head"><span></span>${MONTHS.map((m) => `<b>${m.charAt(0)}</b>`).join("")}</div>
      ${row("catering", "Catered")}${row("self_service", "Self-service")}
    </div>
    <p class="hint">Click a month to cycle: ✓ whole month · ½ part of the month · – no.</p>`;
}

function cycleCode(button) {
  const next = { 2: 0, 0: 1, 1: 2 }[button.dataset.value];
  button.dataset.value = next;
  button.className = `code c${next}${button.classList.contains("edited") ? " edited" : ""}`;
  button.textContent = CODE_LABEL[next];
}

// ---------- Edit an existing hut ----------
export async function editHut(hutId) {
  const res = await fetch(`/api/huts/${hutId}/overrides`);
  if (!res.ok) return;
  editing = { hutId, data: await res.json() };
  const { fields, months, source } = editing.data;
  const inputs = Object.entries(FIELD_LABELS).map(([key, label]) => {
    const f = fields[key];
    const sourceText = f.source === null || f.source === undefined || f.source === "" ? "empty" : esc(f.source);
    return `<label class="edit-field">
        <span>${label}${f.overridden ? ` <button type="button" class="chip-edited" data-reset="${key}" title="Back to the SAC value">edited ×</button>` : ""}</span>
        <input class="input" name="${key}" ${NUMERIC.has(key) ? 'type="number" min="0"' : ""} value="${esc(f.value ?? "")}">
        ${source === "sac_api" ? `<small>SAC: ${sourceText}</small>` : ""}
      </label>`;
  }).join("");
  $("#detail").innerHTML = `
    <div class="tour-top">
      <button class="back flat" data-editor-cancel><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>Cancel</button>
      <h2>Edit ${esc(fields.name.value)}</h2>
      <p class="subtitle">${source === "sac_api" ? "Your changes override the SAC data and survive future updates." : "Hut added by hand."}</p>
    </div>
    <form class="detail-body editor" id="editForm">
      ${inputs}
      <div class="section"><h3>Season</h3>${seasonEditor(months)}</div>
      <label class="edit-field"><span>Note (optional, e.g. where the information comes from)</span>
        <input class="input" name="note" maxlength="500"></label>
      <p class="warn" id="editError" hidden></p>
      <button class="btn primary wide" type="submit">Save</button>
      ${source === "manual" ? `<button class="btn wide danger" type="button" data-delete-hut>Delete this hut</button>` : ""}
    </form>`;
  $("#detail").scrollTop = 0;
}

async function saveEdit(form) {
  const { hutId, data } = editing;
  const note = form.note.value.trim() || null;
  const requests = [];
  for (const key of Object.keys(FIELD_LABELS)) {
    const raw = form[key].value.trim();
    const value = raw === "" ? null : NUMERIC.has(key) ? Number(raw) : raw;
    const f = data.fields[key];
    if (value === (f.value ?? null)) continue;
    requests.push(value === (f.source ?? null) && f.overridden ? reset(hutId, key) : put(hutId, key, value, note));
  }
  form.querySelectorAll(".code").forEach((b) => {
    const m = data.months[b.dataset.kind][b.dataset.month - 1];
    const value = Number(b.dataset.value);
    if (value === m.value) return;
    const field = `${b.dataset.kind}.${b.dataset.month}`;
    requests.push(value === m.source && m.overridden ? reset(hutId, field) : put(hutId, field, value, note));
  });
  const results = await Promise.all(requests);
  const failed = results.find((r) => !r.ok);
  if (failed) {
    $("#editError").hidden = false;
    $("#editError").textContent = await errorText(failed);
    return;
  }
  await afterChange(hutId);
}

const put = (hutId, field, value, note) => fetch(`/api/huts/${hutId}/overrides/${field}`, {
  method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value, note }),
});
const reset = (hutId, field) => fetch(`/api/huts/${hutId}/overrides/${field}`, { method: "DELETE" });

async function afterChange(hutId) {
  editing = null;
  invalidateHut(hutId);
  await reloadHuts();
  selectHut(hutId, { fly: false });
}

// ---------- Add a hut ----------
export function addHut() {
  const blank = { value: 2, source: 2, overridden: false };
  const months = { catering: Array(12).fill(blank), self_service: Array(12).fill(blank) };
  const center = map.getCenter();
  editing = null;
  $("#browse").hidden = true;
  $("#tourDetail").hidden = true;
  $("#detail").hidden = false;
  $("#detail").innerHTML = `
    <div class="tour-top">
      <button class="back flat" data-editor-close><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>Cancel</button>
      <h2>Add a hut</h2>
      <p class="subtitle">For huts that aren't in the SAC data. Walking connections are added at the next
        <code>refresh --rebuild</code>.</p>
    </div>
    <form class="detail-body editor" id="addForm">
      <label class="edit-field"><span>Name</span><input class="input" name="name" required minlength="2" maxlength="120"></label>
      <div class="field two">
        <label class="edit-field"><span>Latitude</span><input class="input" name="lat" type="number" step="any" required value="${center.lat.toFixed(5)}"></label>
        <label class="edit-field"><span>Longitude</span><input class="input" name="lon" type="number" step="any" required value="${center.lng.toFixed(5)}"></label>
      </div>
      <button class="btn" type="button" data-pick>Pick the position on the map</button>
      <div class="field two">
        <label class="edit-field"><span>Altitude (m)</span><input class="input" name="altitude" type="number" min="0"></label>
        <label class="edit-field"><span>Beds</span><input class="input" name="beds" type="number" min="0"></label>
      </div>
      <label class="edit-field"><span>Website</span><input class="input" name="website" placeholder="https://"></label>
      <div class="section"><h3>Season</h3>${seasonEditor(months)}</div>
      <p class="warn" id="editError" hidden></p>
      <button class="btn primary wide" type="submit">Add hut</button>
    </form>`;
  revealPanel();
}

async function submitAdd(form) {
  const num = (name) => (form[name].value.trim() === "" ? null : Number(form[name].value));
  const codes = (kind) => [...form.querySelectorAll(`.code[data-kind="${kind}"]`)].map((b) => b.dataset.value).join("");
  const body = {
    name: form.name.value.trim(), lat: num("lat"), lon: num("lon"), altitude: num("altitude"), beds: num("beds"),
    website: form.website.value.trim() || null, catering: codes("catering"), self_service: codes("self_service"),
  };
  const res = await fetch("/api/huts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    $("#editError").hidden = false;
    if (res.status === 422) {
      const detail = (await res.json()).detail;
      $("#editError").textContent = Array.isArray(detail) ? detail.map((d) => `${d.loc.at(-1)}: ${d.msg}`).join("; ") : detail;
    } else {
      $("#editError").textContent = await errorText(res);
    }
    return;
  }
  const { id } = await res.json();
  await reloadHuts();
  selectHut(id);
}

function pickOnMap(form) {
  map.getCanvas().style.cursor = "crosshair";
  map.once("click", (e) => {
    map.getCanvas().style.cursor = "";
    form.lat.value = e.lngLat.lat.toFixed(5);
    form.lon.value = e.lngLat.lng.toFixed(5);
  });
}

async function deleteHut(button) {
  if (button.dataset.armed !== "1") {  // two clicks instead of a browser confirm dialog
    button.dataset.armed = "1";
    button.textContent = "Click again to delete permanently";
    return;
  }
  const { hutId } = editing;
  const res = await fetch(`/api/huts/${hutId}`, { method: "DELETE" });
  if (!res.ok) {
    button.textContent = await errorText(res);
    return;
  }
  editing = null;
  invalidateHut(hutId);
  await reloadHuts();
  closeDetail();
}

export function bindEditorEvents() {
  const detail = $("#detail");
  detail.addEventListener("click", (e) => {
    const t = e.target;
    if (t.closest("[data-edit-hut]")) { e.preventDefault(); editHut(Number(t.closest("[data-edit-hut]").dataset.editHut)); }
    else if (t.closest(".code")) cycleCode(t.closest(".code"));
    else if (t.closest("[data-reset]")) {
      const key = t.closest("[data-reset]").dataset.reset;
      $("#editForm")[key].value = editing.data.fields[key].source ?? "";
      t.closest("[data-reset]").remove();
    } else if (t.closest("[data-editor-cancel]")) selectHut(editing.hutId, { fly: false });
    else if (t.closest("[data-editor-close]")) closeDetail();
    else if (t.closest("[data-pick]")) pickOnMap($("#addForm"));
    else if (t.closest("[data-delete-hut]")) deleteHut(t.closest("[data-delete-hut]"));
  });
  detail.addEventListener("submit", (e) => {
    e.preventDefault();
    if (e.target.id === "editForm") saveEdit(e.target);
    if (e.target.id === "addForm") submitAdd(e.target);
  });
  $("#addHut").addEventListener("click", addHut);
}

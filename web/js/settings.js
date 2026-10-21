// General settings, kept in this browser (local storage, per device) and sent with every request that offers or
// shows routes: the hardest trail and the highest point a route may reach, whether to avoid start and end stops
// served only by mountain lifts, your walking pace (times scale with it), Alpine Club member prices, whether
// searches check free beds, and metric or imperial units. Anything outside them is never offered.
import { $, esc } from "./util.js";

const KEY = "huthop.settings";
export const SETTING_DEFAULTS = {
  limit: 3, maxAltitude: null, avoidLifts: false, pace: 1, member: true, checkBeds: true, units: "metric",
};
const ALTITUDES = [2000, 2250, 2500, 2750, 3000, 3250];
const PACES = [0.7, 0.8, 0.9, 1, 1.1, 1.2, 1.3, 1.5];
const listeners = [];
let current = load();

function load() {
  try {
    return { ...SETTING_DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || "{}") };
  } catch {
    return { ...SETTING_DEFAULTS };
  }
}

export const settings = () => current;
export const onSettingsChange = (fn) => listeners.push(fn);

export function setSettings(patch) {
  current = { ...current, ...patch };
  try {
    localStorage.setItem(KEY, JSON.stringify(current));
  } catch {
    // storage blocked: the settings last until the page is reloaded
  }
  listeners.forEach((fn) => fn(patch));
}

/** The settings the server applies, as query parameters (also merged into the search body). */
export function settingsParams() {
  const p = { limit: current.limit, avoid_lifts: current.avoidLifts, pace: current.pace };
  if (current.maxAltitude) p.max_altitude = current.maxAltitude;
  return p;
}

export const settingsQuery = () => new URLSearchParams(settingsParams()).toString();

// ---------- Units ----------
const FEET_PER_M = 3.28084;
const M_PER_MILE = 1609.344;
const imperial = () => current.units === "imperial";

/** 4500 → "4.5" (km) or "2.8" (mi); see distanceUnit. */
export const distanceValue = (m, digits = 1) => (m / (imperial() ? M_PER_MILE : 1000)).toFixed(digits);
export const distanceUnit = () => (imperial() ? "mi" : "km");
export const formatDistance = (m, digits = 1) => `${distanceValue(m, digits)} ${distanceUnit()}`;
/** 1234 → "1'234" (m) or "4'049" (ft); see heightUnit. */
export const heightValue = (m) => Math.round(imperial() ? m * FEET_PER_M : m).toLocaleString("de-CH");
export const heightUnit = () => (imperial() ? "ft" : "m");
export const formatHeight = (m) => `${heightValue(m)} ${heightUnit()}`;

// ---------- Dialog ----------
function seg(name, options, value) {
  return `<div class="seg" role="group" data-setting="${name}">${options.map(([v, label]) =>
    `<button type="button" data-v="${v}" aria-pressed="${String(v) === String(value)}">${label}</button>`).join("")}</div>`;
}

function check(name, label) {
  return `<label class="check"><input type="checkbox" data-setting="${name}"${current[name] ? " checked" : ""}> ${label}</label>`;
}

function render() {
  const s = current;
  const pace = (p) => (p === 1 ? "100 % (Swiss hiking times)" : `${Math.round(p * 100)} %${p < 1 ? " (faster)" : " (slower)"}`);
  $("#settings").innerHTML = `
    <div class="about-head">
      <h2>Settings</h2>
      <button class="about-close" data-close aria-label="Close">×</button>
    </div>
    <div class="about-body settings-body">
      <p class="hint">Saved in this browser. Routes outside these settings are never offered: not in tour
        suggestions, the tour editor or a hut's connections.</p>
      <div class="field"><div class="field-label">Hardest trail</div>
        ${seg("limit", [[1, '<i class="trail-swatch c1"></i>Yellow'], [2, '<i class="trail-swatch c2"></i>Red-white'],
          [3, '<i class="trail-swatch c3"></i>Blue-white']], s.limit)}</div>
      <div class="field"><label class="field-label" for="setAltitude">Highest point of a route</label>
        <select class="input" id="setAltitude" data-setting="maxAltitude">
          <option value=""${s.maxAltitude ? "" : " selected"}>No limit</option>
          ${ALTITUDES.map((a) => `<option value="${a}"${a === s.maxAltitude ? " selected" : ""}>Up to ${esc(formatHeight(a))}</option>`).join("")}
        </select></div>
      <div class="field"><label class="field-label" for="setPace">Walking pace</label>
        <select class="input" id="setPace" data-setting="pace">
          ${PACES.map((p) => `<option value="${p}"${p === s.pace ? " selected" : ""}>${pace(p)}</option>`).join("")}
        </select>
        <p class="hint">Times are shown at your pace, and days must fit your hours at it.</p></div>
      <div class="field"><div class="field-label">Units</div>
        ${seg("units", [["metric", "Kilometres, metres"], ["imperial", "Miles, feet"]], s.units)}</div>
      ${check("avoidLifts", "Avoid mountain lifts (cable cars, chairlifts, funiculars) to and from the huts")}
      ${check("member", "Alpine Club member (SAC, DAV, …): member prices in the cost estimate")}
      ${check("checkBeds", "Check free beds on hut-reservation.org when searching")}
      <button class="link inline" id="setReset">Back to the defaults</button>
    </div>`;
}

export function bindSettings() {
  const dialog = $("#settings");
  $("#settingsBtn").addEventListener("click", () => {
    render();
    dialog.showModal();
  });
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog || e.target.closest("[data-close]")) { dialog.close(); return; }
    if (e.target.closest("#setReset")) { setSettings({ ...SETTING_DEFAULTS }); render(); return; }
    const b = e.target.closest(".seg[data-setting] button");
    if (b) {
      const name = b.closest("[data-setting]").dataset.setting;
      setSettings({ [name]: name === "limit" ? Number(b.dataset.v) : b.dataset.v });
      render();
    }
  });
  dialog.addEventListener("change", (e) => {
    const name = e.target.dataset.setting;
    if (!name) return;
    const value = e.target.type === "checkbox" ? e.target.checked
      : name === "maxAltitude" ? (Number(e.target.value) || null) : Number(e.target.value);
    setSettings({ [name]: value });
  });
}

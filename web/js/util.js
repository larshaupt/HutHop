// Shared constants and small helpers (no state, no DOM side effects).
import { LOCALE, t } from "./i18n.js";

export const MONTHS = [...Array(12)].map((_, i) => new Date(2000, i, 1).toLocaleDateString(LOCALE, { month: "short" }));
export const COLORS = { catered: "#2e8b57", self_service: "#2f6fb3", closed: "#9aa3a0", unknown: "#ffffff", any: "#c8102e" };
export const STATUS_LABEL = { catered: t("Catered"), self_service: t("Self-service"), closed: t("Closed"), unknown: t("No data") };
export const CATEGORY = {
  1: { label: t("Yellow"), name: t("Hiking trail"), color: "#e0a800" },
  2: { label: t("Red-white"), name: t("Mountain trail"), color: "#d52b1e" },
  3: { label: t("Blue-white"), name: t("Alpine trail"), color: "#1f5fbf" },
  4: { label: t("Unofficial"), name: t("Unofficial path (OpenStreetMap)"), color: "#7a3e9d" },
};
/** A trail category's name, with the OSM grade (T1–T6) of an unofficial path when known. */
export const categoryName = (category, grade) => CATEGORY[category].name + (grade ? `, ${grade}` : "");

export const MODE_LABELS = { cableway: t("Cable car"), chairlift: t("Chairlift"), funicular: t("Funicular"),
  rack_railway: t("Rack railway"), train: t("Train"), boat: t("Boat"), tram: t("Tram"), metro: t("Metro"), elevator: t("Lift"), bus: t("Bus") };
export const BOOKING_LABEL = { hut_reservation_org: "hut-reservation.org", online_other: t("online"), email: t("by email"),
  phone: t("by phone"), not_needed: t("not needed") };
export const EMPTY = { type: "FeatureCollection", features: [] };

export const $ = (sel) => document.querySelector(sel);
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export const isMobile = () => matchMedia("(max-width: 720px)").matches;

/** A date as YYYY-MM-DD in local time (toISOString alone would give the UTC date). */
export const localIso = (d = new Date()) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
/** Phone number as a tel: link (digits and + only). */
export const telHref = (phone) => `tel:${phone.replace(/[^\d+]/g, "")}`;

export function setSheet(mode) {
  $("#panel").dataset.sheet = mode;
}

/** Expand the mobile bottom sheet if it is collapsed. */
export function revealPanel() {
  if (isMobile() && $("#panel").dataset.sheet === "peek") setSheet("half");
}

export function formatMinutes(min) {
  if (min == null) return "–";
  const h = Math.floor(min / 60), m = min % 60;
  return `${h}:${String(m).padStart(2, "0")} h`;
}

export function formatDuration(min, max) {
  const fmt = (m) => {
    const h = Math.floor(m / 60), r = m % 60;
    return r ? `${h}:${String(r).padStart(2, "0")}` : `${h}`;
  };
  if (!min && !max) return null;
  if (min && max && min !== max) return `${fmt(min)}–${fmt(max)} h`;
  return `${fmt(max || min)} h`;
}

/** Polyline-encoded whole numbers (as in Google's polyline format) → [n, …]. */
function decodeInts(text) {
  const values = [];
  let index = 0;
  while (index < text.length) {
    let shift = 0, result = 0, b;
    do {
      b = text.charCodeAt(index++) - 63;
      result |= (b & 0x1f) << shift;
      shift += 5;
    } while (b >= 0x20);
    values.push(result & 1 ? ~(result >> 1) : result >> 1);
  }
  return values;
}

/** Encoded polyline (precision 5) → [[lon, lat], …]. Each value is the change from the previous point. */
export function decodePolyline(text) {
  const d = decodeInts(text), points = [];
  for (let i = 0, lat = 0, lon = 0; i + 1 < d.length; i += 2) points.push([(lon += d[i + 1]) / 1e5, (lat += d[i]) / 1e5]);
  return points;
}

/** Encoded whole numbers (heights in metres, one per route point; see app/planner/polyline.py) → [z, …]. */
export function decodeValues(text) {
  let v = 0;
  return decodeInts(text).map((d) => (v += d));
}

/** Distance in metres between two [lon, lat] points (haversine). */
export function distanceM([lon1, lat1], [lon2, lat2]) {
  const rad = Math.PI / 180, dLat = (lat2 - lat1) * rad, dLon = (lon2 - lon1) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 12742000 * Math.asin(Math.sqrt(h));
}

/** The most relevant transport mode of a stop, for labels. */
export function mainMode(mode) {
  const modes = (mode || "").split(",");
  return Object.keys(MODE_LABELS).find((m) => modes.includes(m)) || "bus";
}

export const modeLabel = (mode) => MODE_LABELS[mainMode(mode)];

export function categoryColor() {
  return ["match", ["get", "category"], 1, CATEGORY[1].color, 2, CATEGORY[2].color, 4, CATEGORY[4].color,
    CATEGORY[3].color];
}

/** A tour's estimated hut costs (cost.totals: per currency, member and regular price per person) as text, e.g.
 * "CHF 245" or "CHF 180 + EUR 60"; `factor` multiplies (number of people). */
export function formatCost(totals, member, factor = 1) {
  return Object.entries(totals).map(([cur, t]) => `${cur} ${Math.round((member ? t.member : t.non_member) * factor)}`)
    .join(" + ");
}

/** Hover text explaining a tour's cost estimate. */
export function costTitle(tour, member, people) {
  const c = tour.cost;
  return [`${t(member ? "Estimated hut costs per person (member prices)" : "Estimated hut costs per person")}: ${t("cheapest bed, half board when catered,")}`,
    t("kitchen fee when self-service, tourist tax. Drinks and lunches not included."),
    `${t("{priced} of {n} nights with prices from the hut websites", { priced: c.priced, n: tour.nights })}${c.years.length ? ` (${c.years.join(", ")})` : ""}.`,
    people > 1 ? t("For {n} people: {cost}.", { n: people, cost: formatCost(c.totals, member, people) }) : ""].filter(Boolean).join("\n");
}

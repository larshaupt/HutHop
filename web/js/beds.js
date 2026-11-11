// Free-beds calendar in the hut detail: one month at a time from hut-reservation.org (via /api/huts/{id}/availability).
import { $, esc } from "./util.js";
import { setState, state } from "./state.js";
import { checkedAgo } from "./tour-detail.js";
import { LOCALE, t } from "./i18n.js";

let beds = null; // { id, data, month: "YYYY-MM" } for the free-beds calendar of the open hut
// 2024-01-01 was a Monday
const WEEKDAYS = [...Array(7)].map((_, i) => new Date(2024, 0, 1 + i).toLocaleDateString(LOCALE, { weekday: "short" }).slice(0, 2));
const MONTH_FMT = new Intl.DateTimeFormat(LOCALE, { month: "long", year: "numeric" });
const BED_TITLE = { available: t("beds free"), full: t("full"), closed: t("closed"), not_released: t("not bookable yet") };
let seq = 0;

export async function loadBeds(id) {
  const mine = ++seq;
  let data = null;
  try {
    const res = await fetch(`/api/huts/${id}/availability?people=${state.tPeople}`);
    if (res.ok) data = await res.json();
  } catch (err) {
    console.error(err);
  }
  if (mine !== seq || state.hut !== id || !$("#bedCal")) return; // another hut or group size was asked for meanwhile
  if (!data || data.error) {
    $("#bedCal").innerHTML = `<p class="hint">${t("Could not check free beds right now.")}</p>`;
    return;
  }
  if (!data.days.length) {
    $("#bedCal").innerHTML = `<p class="hint">${t("No dates open for online booking yet.")}</p>`;
    return;
  }
  // Start at the planned tour month if the calendar covers it, else at the first month with released places.
  const months = [...new Set(data.days.map((d) => d.date.slice(0, 7)))];
  const tourMonth = state.tDate.slice(0, 7);
  const firstOpen = data.days.find((d) => d.total_places)?.date.slice(0, 7);
  const keep = beds?.id === id && months.includes(beds.month) ? beds.month : null;
  beds = { id, data, month: keep || (months.includes(tourMonth) ? tourMonth : firstOpen || months[0]) };
  renderBeds();
}

function renderBeds() {
  const { data, month } = beds;
  const months = [...new Set(data.days.map((d) => d.date.slice(0, 7)))];
  const byDate = new Map(data.days.map((d) => [d.date, d]));
  const [y, m] = month.split("-").map(Number);
  const first = new Date(y, m - 1, 1), daysInMonth = new Date(y, m, 0).getDate();
  const cells = Array((first.getDay() + 6) % 7).fill(`<span></span>`);
  for (let day = 1; day <= daysInMonth; day++) {
    const iso = `${month}-${String(day).padStart(2, "0")}`;
    const d = byDate.get(iso);
    if (!d) { cells.push(`<span class="bd past">${day}</span>`); continue; }
    const free = d.total_places ? `<small>${d.free_beds ?? 0}</small>` : "";
    const title = `${iso}: ${d.total_places ? `${t("{free} of {total}", { free: d.free_beds ?? 0, total: d.total_places })} ` : ""}${BED_TITLE[d.state] || d.state}`;
    const self = d.state === "available" && d.hut_status === "UNSERVICED" ? " self" : ""; // winter room / no staff
    cells.push(`<span class="bd ${d.state}${self}" title="${title}${self ? ` (${t("self-service")})` : ""}">${day}${free}</span>`);
  }
  const i = months.indexOf(month);
  $("#bedCal").innerHTML = `
    <div class="cal-head">
      <button class="cal-nav" data-cal="-1" ${i <= 0 ? "disabled" : ""} aria-label="${t("Previous month")}">‹</button>
      <b>${MONTH_FMT.format(first)}</b>
      <button class="cal-nav" data-cal="1" ${i >= months.length - 1 ? "disabled" : ""} aria-label="${t("Next month")}">›</button>
    </div>
    <div class="cal">${WEEKDAYS.map((w) => `<b>${w}</b>`).join("")}${cells.join("")}</div>
    <div class="season-legend">
      <span><i class="dot bd-ok"></i>${t("{n}+ beds free", { n: data.people })}</span>${data.days.some((d) => d.hut_status === "UNSERVICED")
        ? `<span><i class="dot bd-self"></i>${t("Self-service")}</span>` : ""}<span><i class="dot bd-full"></i>${t("Full")}</span>
      <span><i class="dot closed"></i>${t("Closed")}</span><span><i class="dot unknown"></i>${t("Not bookable yet")}</span>
    </div>
    <p class="hint cal-foot">${t("For")} <span class="people-step"><button data-people-cal="-1" aria-label="${t("Fewer people")}">−</button>
      <b>${data.people}</b><button data-people-cal="1" aria-label="${t("More people")}">+</button></span>
      ${t("people", { n: data.people })} · ${t("numbers are free beds")}${checkedAgo(data)} ·
      <a href="${esc(data.booking_url)}" target="_blank" rel="noopener">${t("Book")}</a></p>`;
  $("#bedCal").onclick = (e) => {
    const nav = e.target.closest("[data-cal]");
    if (nav) { beds.month = months[i + Number(nav.dataset.cal)]; renderBeds(); return; }
    const step = e.target.closest("[data-people-cal]");
    if (step) {
      setState({ tPeople: Math.min(30, Math.max(1, state.tPeople + Number(step.dataset.peopleCal))) });
      loadBeds(beds.id);
    }
  };
}

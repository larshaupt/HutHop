// Free-beds calendar in the hut detail: one month at a time from hut-reservation.org (via /api/huts/{id}/availability).
import { $, esc } from "./util.js";
import { setState, state } from "./state.js";

let beds = null; // { id, data, month: "YYYY-MM" } for the free-beds calendar of the open hut
const WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const MONTH_FMT = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric" });
const BED_TITLE = { available: "beds free", full: "full", closed: "closed", not_released: "not bookable yet" };

export async function loadBeds(id) {
  let data = null;
  try {
    const res = await fetch(`/api/huts/${id}/availability?people=${state.tPeople}`);
    if (res.ok) data = await res.json();
  } catch (err) {
    console.error(err);
  }
  if (state.hut !== id || !$("#bedCal")) return; // another hut was opened meanwhile
  if (!data || data.error) {
    $("#bedCal").innerHTML = `<p class="hint">Could not check free beds right now.</p>`;
    return;
  }
  if (!data.days.length) {
    $("#bedCal").innerHTML = `<p class="hint">No dates open for online booking yet.</p>`;
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
    const title = `${iso}: ${d.total_places ? `${d.free_beds ?? 0} of ${d.total_places} ` : ""}${BED_TITLE[d.state] || d.state}`;
    const self = d.state === "available" && d.hut_status === "UNSERVICED" ? " self" : ""; // winter room / no staff
    cells.push(`<span class="bd ${d.state}${self}" title="${title}${self ? " (self-service)" : ""}">${day}${free}</span>`);
  }
  const i = months.indexOf(month);
  const ago = Math.max(0, Math.round((Date.now() - new Date(data.checked_at)) / 60000));
  $("#bedCal").innerHTML = `
    <div class="cal-head">
      <button class="cal-nav" data-cal="-1" ${i <= 0 ? "disabled" : ""} aria-label="Previous month">‹</button>
      <b>${MONTH_FMT.format(first)}</b>
      <button class="cal-nav" data-cal="1" ${i >= months.length - 1 ? "disabled" : ""} aria-label="Next month">›</button>
    </div>
    <div class="cal">${WEEKDAYS.map((w) => `<b>${w}</b>`).join("")}${cells.join("")}</div>
    <div class="season-legend">
      <span><i class="dot bd-ok"></i>${data.people}+ beds free</span>${data.days.some((d) => d.hut_status === "UNSERVICED")
        ? `<span><i class="dot bd-self"></i>Self-service</span>` : ""}<span><i class="dot bd-full"></i>Full</span>
      <span><i class="dot closed"></i>Closed</span><span><i class="dot unknown"></i>Not bookable yet</span>
    </div>
    <p class="hint cal-foot">For <span class="people-step"><button data-people-cal="-1" aria-label="Fewer people">−</button>
      <b>${data.people}</b><button data-people-cal="1" aria-label="More people">+</button></span>
      ${data.people === 1 ? "person" : "people"} · numbers are free beds · checked ${ago < 1 ? "just now" : `${ago} min ago`} ·
      <a href="${esc(data.booking_url)}" target="_blank" rel="noopener">Book</a></p>`;
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

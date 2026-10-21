// Tour detail view: day-by-day timeline with nights, free beds and a height profile per day.
import { $, CATEGORY, costTitle, decodePolyline, decodeValues, distanceM, esc, formatCost, formatMinutes, modeLabel }
  from "./util.js";
import { state } from "./state.js";

const NIGHT_LABEL = { catered: "Catered", self_service: "Self-service", unknown: "No opening data", closed: "Closed" };
const AVAIL_LABEL = {
  available: (a) => `${a.free_beds} beds free`,
  full: (a) => `Full (${a.free_beds ?? 0} beds free)`,
  closed: () => "Closed",
  not_released: () => "Not bookable yet",
  no_booking: () => "No online booking",
  not_checked: () => "Free beds not checked",
  error: () => "Could not check free beds",
};
const DAY_FMT = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" });

const formatDay = (iso) => DAY_FMT.format(new Date(`${iso}T12:00:00`));

function stopLabel(p) {
  return `<span class="mode">${modeLabel(p.mode)}</span> ${esc(p.name)}`;
}

/** Distance (m) and height (m) of each route point of a day, or null without a height profile. */
function dayProfile(d) {
  const points = decodePolyline(d.geometry || ""), heights = decodeValues(d.elevation || "");
  if (points.length < 2 || heights.length !== points.length) return null;
  let dist = 0;
  return points.map((p, i) => [(dist += i ? distanceM(points[i - 1], p) : 0), heights[i]]);
}

/** Small height profile of one day. All days share the height scale, and widths follow the day's distance. */
function profileChart(profile, scale, color) {
  if (!profile) return "";
  const W = 300, H = 52, length = profile[profile.length - 1][0];
  const x = (m) => ((m / length) * W).toFixed(1);
  const y = (z) => (H - 2 - ((z - scale.min) / (scale.max - scale.min || 1)) * (H - 4)).toFixed(1);
  const line = profile.map(([m, z], i) => `${i ? "L" : "M"}${x(m)},${y(z)}`).join("");
  const zs = profile.map(([, z]) => z);
  const hi = Math.max(...zs), lo = Math.min(...zs);
  return `<div class="profile" title="Height profile: ${lo.toLocaleString("de-CH")}–${hi.toLocaleString("de-CH")} m">
      <div class="profile-plot"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:${Math.max(30, (length / scale.length) * 100)}%">
        <path d="${line}L${W},${H}L0,${H}Z" fill="${color}" opacity=".14"/>
        <path d="${line}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
      </svg></div>
      <div class="profile-axis"><span>${hi.toLocaleString("de-CH")} m</span><span>${lo.toLocaleString("de-CH")} m</span></div>
    </div>`;
}

/** First or last connection of the day at the tour's start or end stop, with a warning when it is tight. */
function connectionLine(s, role) {
  if (!s) return "";
  if (!s.known) return `<div class="tl-meta transit warn-text">No timetable for this date yet – check connections</div>`;
  if (!s.calls) return `<div class="tl-meta transit warn-text">No public transport on this day</div>`;
  if (role === "access") {
    return `<div class="tl-meta transit${s.late ? " warn-text" : ""}">First connection ${s.first} · at the hut from about
      ${s.hut_from}${s.late ? " – late for dinner, plan an earlier arrival" : ""}</div>`;
  }
  return `<div class="tl-meta transit${s.early ? " warn-text" : ""}">Last connection ${s.last} · leave the hut by
    ${s.leave_by}${s.early ? " – a very early start" : ""}</div>`;
}

function stopChooser(role) {
  return `<button class="link inline" data-stops="${role}">Other stops</button>
    <ul class="stop-options" id="stopOptions-${role}" hidden></ul>`;
}

/** The list of other start or end stops (from /api/tours/{id}/stops); a click switches the tour to that stop. */
export function renderStopOptions(role, options) {
  const box = $(`#stopOptions-${role}`);
  if (!box) return;
  if (!options.length) { box.innerHTML = `<li class="hint">No other stop with public transport on this day.</li>`; return; }
  box.innerHTML = options.map((o) => {
    const s = o.service;
    const when = !s.known ? "no timetable yet"
      : role === "access" ? `${s.calls} connections · first ${s.first}${s.late ? " (late)" : ""}`
        : `${s.calls} connections · last ${s.last}${s.early ? " (early)" : ""}`;
    return `<li><button class="stop-opt${o.current ? " current" : ""}" data-tour-id="${esc(o.tour_id)}"${o.current ? " aria-current=\"true\"" : ""}>
      <span class="stop-opt-name">${stopLabel(o.stop)}</span>
      <span class="stop-opt-meta">${formatMinutes(o.walk.time_min)} walk · ↑ ${o.walk.ascent_m} m · ↓ ${o.walk.descent_m} m ·
        <span style="color:${CATEGORY[o.walk.max_category].color}">${CATEGORY[o.walk.max_category].name}</span> · ${when} ·
        score ${Math.round(o.score * 100)}%</span></button></li>`;
  }).join("");
}

export function renderTourDetail(t) {
  const start = t.days[0].from, end = t.days[t.days.length - 1].to;
  const profiles = t.days.map(dayProfile);
  const heights = profiles.filter(Boolean).flatMap((p) => p.map(([, z]) => z));
  const scale = { min: Math.min(...heights), max: Math.max(...heights),
    length: Math.max(...profiles.filter(Boolean).map((p) => p[p.length - 1][0])) };
  const rows = t.days.map((d, i) => {
    const cat = CATEGORY[d.max_category];
    const sac = d.sac_time_min ? ` · SAC ${formatMinutes(d.sac_time_min)}` : "";
    const from = d.from.kind === "stop" ? stopLabel(d.from) : esc(d.from.name);
    const to = d.to.kind === "stop" ? stopLabel(d.to) : `<a href="#" data-hut="${d.to.id}">${esc(d.to.name)}</a>`;
    const night = d.night ? `<li class="tl-night">
        <div class="tl-dot hut"><span>${d.day}</span></div>
        <div><b><a href="#" data-hut="${d.to.id}">${esc(d.to.name)}</a></b>
          <div class="tl-meta">Night of ${formatDay(d.date)} ·
            <span class="night ${d.night.status}">${NIGHT_LABEL[d.night.status]}${d.night.partial ? ", part of the month – check dates" : ""}</span></div>
          ${nightAvailability(d.night)}
          ${nightCost(d.night.cost)}
        </div></li>` : "";
    return `<li class="tl-day" data-day="${d.day}">
        <div class="tl-dot" style="--c:${cat.color}"></div>
        <div><div class="tl-title">Day ${d.day} · ${formatDay(d.date)}</div>
          <div>${from} → ${to}</div>
          <div class="tl-meta"><b>${formatMinutes(d.time_min)}</b> · ${(d.distance_m / 1000).toFixed(1)} km ·
            ↑ ${d.ascent_m} m · ↓ ${d.descent_m} m · <span style="color:${cat.color}">${cat.name}</span>${sac}</div>
          ${i === 0 ? connectionLine(t.access, "access") + stopChooser("access") : ""}
          ${i === t.days.length - 1 ? connectionLine(t.exit, "exit") + stopChooser("exit") : ""}
          ${profileChart(profiles[i], scale, cat.color)}
        </div></li>${night}`;
  }).join("");
  $("#tourDetail").innerHTML = `
    <div class="tour-top">
      <button class="back flat" id="tourBack"><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>Tours</button>
      <h2>${esc(start.name)} → ${esc(end.name)}</h2>
      <div class="subtitle">${t.nights} night${t.nights > 1 ? "s" : ""}, ${t.days.length} days ·
        ${formatDay(t.days[0].date)} – ${formatDay(t.days[t.days.length - 1].date)}</div>
      <div class="facts">
        <div class="fact"><b>${formatMinutes(t.time_min).replace(" h", "")}</b><span>hours walking</span></div>
        <div class="fact"><b>${(t.distance_m / 1000).toFixed(1)}</b><span>km</span></div>
        <div class="fact"><b>${t.ascent_m.toLocaleString("de-CH")}</b><span>m ascent</span></div>
      </div>
      ${tourCost(t)}
      <div class="actions">
        <a class="btn" href="/api/tours/${encodeURIComponent(t.id)}/gpx" download>Download GPX</a>
      </div>
    </div>
    <div class="detail-body">
      <ol class="timeline">${rows}</ol>
      <p class="source">Walking times use the Swiss hiking-time formula without breaks. Opening data from the SAC;
        free beds from hut-reservation.org (cached up to 10 minutes; not a reservation). Check conditions, weather
        and official sources before you go.</p>
    </div>`;
}

function nightCost(c) {
  if (!c) return `<div class="tl-meta">No prices known for this hut</div>`;
  const amount = state.tMember ? c.member : c.non_member;
  return `<div class="tl-meta">≈ ${c.currency} ${Math.round(amount)} per person: ${c.basis}` +
    `${c.tax ? " + tax" : ""}${c.label ? ` (${esc(c.label)}` + (c.year ? `, ${c.year})` : ")") : ""}</div>`;
}

/** Estimated hut costs for the tour: per person and for the group, with how many nights have prices. */
function tourCost(t) {
  if (!t.cost?.priced) return `<p class="hint">No hut prices known for this tour.</p>`;
  const people = state.tPeople;
  const from = t.cost.priced < t.nights ? "from " : "";
  return `<p class="tour-cost" title="${esc(costTitle(t, state.tMember, people))}">Hut costs ≈ <b>${from}` +
    `${formatCost(t.cost.totals, state.tMember)}</b> per person${people > 1 ? `, ${from}${formatCost(t.cost.totals, state.tMember, people)} for ${people}` : ""}` +
    `${t.cost.priced < t.nights ? ` · ${t.nights - t.cost.priced} night${t.nights - t.cost.priced > 1 ? "s" : ""} without prices` : ""}</p>`;
}

function nightAvailability(n) {
  const a = n.availability;
  if (!a) return "";
  const ago = a.checked_at ? Math.max(0, Math.round((Date.now() - new Date(a.checked_at)) / 60000)) : null;
  const when = ago === null ? "" : ago < 1 ? " · checked just now" : ` · checked ${ago} min ago`;
  const book = n.booking_url ? ` <a class="book" href="${esc(n.booking_url)}" target="_blank" rel="noopener">Book</a>` : "";
  return `<div class="tl-meta avail ${a.state}">${AVAIL_LABEL[a.state](a)}${a.state === "available" ? when : ""}${book}</div>`;
}

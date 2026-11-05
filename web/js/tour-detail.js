// Tour detail view: day-by-day timeline with nights, free beds and a height profile per day.
import { $, CATEGORY, categoryName, costTitle, decodePolyline, decodeValues, distanceM, esc, formatCost, formatMinutes, modeLabel }
  from "./util.js";
import { state } from "./state.js";
import { bookingPanel, isBooked } from "./booking.js";
import { hutForecast, weatherLine } from "./weather.js";
import { distanceUnit, distanceValue, formatDistance, formatHeight, heightUnit, heightValue, settings } from "./settings.js";

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
  return `<div class="profile" title="Height profile: ${heightValue(lo)}–${formatHeight(hi)}">
      <div class="profile-plot"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:${Math.max(30, (length / scale.length) * 100)}%">
        <path d="${line}L${W},${H}L0,${H}Z" fill="${color}" opacity=".14"/>
        <path d="${line}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
      </svg></div>
      <div class="profile-axis"><span>${formatHeight(hi)}</span><span>${formatHeight(lo)}</span></div>
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

export { AVAIL_LABEL, connectionLine, formatDay, stopLabel, NIGHT_LABEL };

export function renderTourDetail(t, { kept = false, booking = false, people = 2, nav = null } = {}) {
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
          ${nightAvailability(d)}
          ${nightCost(d.night.cost)}
        </div></li>` : "";
    return `<li class="tl-day" data-day="${d.day}">
        <div class="tl-dot" style="--c:${cat.color}"></div>
        <div><div class="tl-title">Day ${d.day} · ${formatDay(d.date)}</div>
          <div>${from} → ${to}</div>
          <div class="tl-meta"><b>${formatMinutes(d.time_min)}</b> · ${formatDistance(d.distance_m)} ·
            ↑ ${formatHeight(d.ascent_m)} · ↓ ${formatHeight(d.descent_m)} · <span style="color:${cat.color}">${categoryName(d.max_category, d.osm_grade)}</span>${sac}</div>
          ${i === 0 ? connectionLine(t.access, "access") : ""}
          ${i === t.days.length - 1 ? connectionLine(t.exit, "exit") : ""}
          <div data-day-notes="${d.day}"></div>
          ${profileChart(profiles[i], scale, cat.color)}
        </div></li>${night}`;
  }).join("");
  $("#tourDetail").innerHTML = `
    <div class="tour-top">
      <div class="tour-nav">
        <button class="back flat" id="tourBack"><svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>${state.view === "mine" ? "My tours" : "Tours"}</button>
        ${nav ? `<div class="tour-step">
          <button class="btn" data-step-tour="-1" aria-label="Previous tour"${nav.n > 1 ? "" : " disabled"}>‹</button>
          <span>${nav.n} / ${nav.total}</span>
          <button class="btn" data-step-tour="1" aria-label="Next tour"${nav.n < nav.total ? "" : " disabled"}>›</button>
        </div>` : ""}
      </div>
      <h2>${esc(start.name)} → ${esc(end.name)}</h2>
      <div class="subtitle">${t.nights} night${t.nights > 1 ? "s" : ""}, ${t.days.length} days ·
        ${formatDay(t.days[0].date)} – ${formatDay(t.days[t.days.length - 1].date)}</div>
      <div class="facts">
        <div class="fact"><b>${formatMinutes(t.time_min).replace(" h", "")}</b><span>hours walking</span></div>
        <div class="fact"><b>${distanceValue(t.distance_m)}</b><span>${distanceUnit()}</span></div>
        <div class="fact"><b>${heightValue(t.ascent_m)}</b><span>${heightUnit()} ascent</span></div>
      </div>
      ${outsideSettings(t)}
      ${unofficialWarning(t)}
      ${tourCost(t)}
      <div class="actions">
        <button class="btn primary" id="tourEdit">Edit tour</button>
        <button class="btn" id="tourBook" aria-pressed="${booking}">Book this tour</button>
        <button class="btn" id="tourKeep" aria-pressed="${kept}">${kept ? "★ In my tours" : "☆ Add to my tours"}</button>
        <a class="btn" href="/api/tours/${encodeURIComponent(t.id)}/gpx" download>Download GPX</a>
      </div>
    </div>
    <div class="detail-body">
      ${booking ? bookingPanel(t, people) : ""}
      <ol class="timeline">${rows}</ol>
      <p class="source">Walking times use the Swiss hiking-time formula without breaks. Opening data from the SAC;
        free beds from hut-reservation.org (cached up to 10 minutes; not a reservation). Weather: MeteoSwiss forecast
        (via Open-Meteo) at the day's hut, for the next 5 days. Trail closures (Swiss Hiking Federation) and military
        shooting notices from map.geo.admin.ch. Check conditions, weather and official sources before you go.</p>
    </div>`;
  dayNotes(t);
}

const warnings = new Map(); // tour id → promise of { days, error }, for the page's life (the server caches an hour)

/** Weather and official warnings per day, filled in when they arrive (the tour shows without them). */
async function dayNotes(t) {
  const el = (day) => document.querySelector(`#tourDetail [data-day-notes="${day}"]`);
  if (!warnings.has(t.id)) {
    warnings.set(t.id, fetch(`/api/tours/${encodeURIComponent(t.id)}/warnings`)
      .then((r) => (r.ok ? r.json() : { days: [], error: true })).catch(() => ({ days: [], error: true })));
  }
  const weatherOf = (d) => {
    const hut = d.to.kind === "hut" ? d.to : d.from;  // the last day: where it starts
    return hutForecast(hut.id).then((f) => f.days.find((w) => w.date === d.date));
  };
  const [found, ...forecast] = await Promise.all([warnings.get(t.id), ...t.days.map(weatherOf)]);
  t.days.forEach((d, i) => {
    const target = el(d.day);
    if (!target) return;  // another tour or view meanwhile
    const w = found.days.find((x) => x.day === d.day) || { closures: [], shooting: [] };
    const link = (url, text) => (/^https?:\/\//i.test(url || "") ? ` · <a href="${esc(url)}" target="_blank" rel="noopener">${text}</a>` : "");
    target.innerHTML = [
      forecast[i] && `<div class="tl-meta" title="Forecast at ${esc(d.to.kind === "hut" ? d.to.name : d.from.name)}">${weatherLine(forecast[i])}</div>`,
      ...w.closures.map((c) => `<div class="tl-meta warn-text">⚠ Trail closure: ${esc(c.title)}` +
        `${c.reason ? ` – ${esc(c.reason)}` : ""}${c.duration ? ` (${esc(c.duration)})` : ""}${c.details ? `. ${esc(c.details)}` : ""}` +
        `${link(c.url, "details")}</div>`),
      ...w.shooting.map((z) => `<div class="tl-meta${z.times.length ? " warn-text" : ""}">` +
        `${z.times.length ? `⚠ Military shooting on this day, ${esc(z.times.join(", "))}` : "Crosses a military danger zone, no shooting published for this day"}` +
        `: ${esc(z.name)}${z.places.length ? ` (${esc(z.places.join(", "))})` : ""}${link(z.url, "shooting notice")}</div>`),
    ].filter(Boolean).join("");
  });
  if (found.error) el(1)?.insertAdjacentHTML("beforeend", `<div class="tl-meta">Could not check trail closures and shooting notices right now.</div>`);
}

function nightCost(c) {
  if (!c) return `<div class="tl-meta">No prices known for this hut</div>`;
  const amount = settings().member ? c.member : c.non_member;
  return `<div class="tl-meta">≈ ${c.currency} ${Math.round(amount)} per person: ${c.basis}` +
    `${c.tax ? " + tax" : ""}${c.label ? ` (${esc(c.label)}` + (c.year ? `, ${c.year})` : ")") : ""}</div>`;
}

/** Estimated hut costs for the tour: per person and for the group, with how many nights have prices. */
function tourCost(t) {
  if (!t.cost?.priced) return `<p class="hint">No hut prices known for this tour.</p>`;
  const people = state.tPeople;
  const from = t.cost.priced < t.nights ? "from " : "";
  return `<p class="tour-cost" title="${esc(costTitle(t, settings().member, people))}">Hut costs ≈ <b>${from}` +
    `${formatCost(t.cost.totals, settings().member)}</b> per person${people > 1 ? `, ${from}${formatCost(t.cost.totals, settings().member, people)} for ${people}` : ""}` +
    `${t.cost.priced < t.nights ? ` · ${t.nights - t.cost.priced} night${t.nights - t.cost.priced > 1 ? "s" : ""} without prices` : ""}</p>`;
}

/** A tour made earlier or shared by someone else can use what your settings wouldn't offer: say what. */
export function outsideSettings(t) {
  const reasons = { trail: "a harder trail", altitude: "a route higher than your altitude limit",
    lift: "a mountain lift at the start or end" };
  const list = (t.outside_settings || []).map((r) => reasons[r]).filter(Boolean);
  return list.length ? `<p class="warn">Outside your settings (⚙): this tour uses ${list.join(" and ")}.</p>` : "";
}

/** Days on unofficial OSM paths: not signposted or maintained, often glacier or alpine terrain. */
function unofficialWarning(t) {
  const days = t.days.filter((d) => d.max_category === 4);
  if (!days.length) return "";
  const which = days.map((d) => `day ${d.day}${d.osm_grade ? ` (${d.osm_grade})` : ""}`).join(", ");
  return `<p class="warn">Unofficial path on ${which}: from OpenStreetMap, not signposted or maintained, often across
    glaciers or alpine terrain. Check the route, conditions and the hut's access description before you go.</p>`;
}

function nightAvailability(d) {
  if (isBooked(d)) return `<div class="tl-meta avail available">✓ Booked</div>`;
  const a = d.night.availability;
  if (!a) return "";
  return `<div class="tl-meta avail ${a.state}">${AVAIL_LABEL[a.state](a)}${a.state === "available" ? checkedAgo(a) : ""}</div>`;
}

/** " · checked N min ago" for a night's free beds. */
export function checkedAgo(a) {
  if (!a.checked_at) return "";
  const min = Math.max(0, Math.round((Date.now() - new Date(a.checked_at)) / 60000));
  return min < 1 ? " · checked just now" : ` · checked ${min} min ago`;
}

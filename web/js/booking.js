// Booking helper ("Book this tour"): a checklist of the tour's nights. HutHop doesn't book: each night opens the
// hut's booking page on hut-reservation.org, or shows how else to reach the hut, and you tick it off once booked.
// Ticks are kept in this browser (local storage, per device) per hut and night, so they stay with a tour you edit.
import { BOOKING_LABEL, esc, telHref } from "./util.js";
import { AVAIL_LABEL, checkedAgo, formatDay } from "./tour-detail.js";
import { t as tr } from "./i18n.js";  // t is a tour here

const KEY = "huthop.booked";

function read() {
  try {
    const marks = JSON.parse(localStorage.getItem(KEY) || "{}");
    return marks && typeof marks === "object" ? marks : {};
  } catch {
    return {};
  }
}

const markKey = (d) => `${d.to.id}_${d.date}`;
export const isBooked = (d) => !!read()[markKey(d)];

export function setBooked(key, booked) {
  const marks = read();
  if (booked) marks[key] = new Date().toISOString();
  else delete marks[key];
  try {
    localStorage.setItem(KEY, JSON.stringify(marks));
  } catch {
    // storage full or blocked: the tick lives only until the page is reloaded
  }
}

/** The checklist, shown above the timeline while "Book this tour" is pressed. */
export function bookingPanel(t, people) {
  const nights = t.days.filter((d) => d.night);
  const open = nights.filter((d) => !isBooked(d));
  const next = open.find((d) => d.night.booking_url);
  const done = nights.length - open.length;
  const nextAction = next
    ? `<a class="btn primary" href="${esc(next.night.booking_url)}" target="_blank" rel="noopener">${tr("Open next:")}
        ${esc(next.to.name)} ↗</a>`
    : open.length ? `<span class="hint">${tr("Book the remaining nights with the huts directly.")}</span>`
      : `<span class="booked-all">✓ ${tr("All nights booked")}</span>`;
  return `<section class="booking">
      <div class="booking-head"><h3>${tr("Book this tour")}</h3><span>${tr("{done} of {n} nights booked", { done, n: nights.length })}</span></div>
      <p class="hint">${tr("HutHop doesn't book for you. Open each hut's booking page, choose the night shown here for {n} people, then tick it off. Ticks stay in this browser.", { n: people })}</p>
      <div class="actions">${nextAction}<button class="btn" id="bookRecheck">${tr("Check free beds again")}</button></div>
      <ol class="book-list">${nights.map(bookingRow).join("")}</ol>
    </section>`;
}

function bookingRow(d) {
  const n = d.night, a = n.availability, booked = isBooked(d);
  const state = a && a.state !== "no_booking" ? `<div class="tl-meta avail ${a.state}">${AVAIL_LABEL[a.state](a)}${checkedAgo(a)}</div>` : "";
  const gone = !booked && a && (a.state === "full" || a.state === "closed")
    ? `<div class="tl-meta warn-text">${tr("Pick another hut for this night in the editor.")}</div>` : "";
  return `<li class="${booked ? "booked" : ""}">
      <label><input type="checkbox" data-booked="${markKey(d)}"${booked ? " checked" : ""}>
        <span><b>${tr("Night {day}", { day: d.day })} · ${formatDay(d.date)}</b> · <a href="#" data-hut="${d.to.id}">${esc(d.to.name)}</a></span></label>
      ${booked ? "" : state + gone}
      ${booked ? "" : `<div class="book-links">${bookingLinks(n)}</div>`}
    </li>`;
}

/** hut-reservation.org where the hut is on it; otherwise what its website says, its website, email and phone. */
function bookingLinks(n) {
  const link = (href, text, external = true) =>
    `<a href="${esc(href)}"${external ? ` target="_blank" rel="noopener"` : ""}>${text}</a>`;
  if (n.booking_url) return link(n.booking_url, `${tr("Booking page")} ↗`);
  const c = n.contact || {};
  const methods = c.booking_methods?.filter((m) => m !== "hut_reservation_org").map((m) => BOOKING_LABEL[m]).filter(Boolean) || [];
  const links = [
    /^https?:\/\//i.test(c.booking_url) && link(c.booking_url, `${tr("Booking page")} ↗`),
    c.website && link(c.website, `${tr("Website")} ↗`),
    c.email && link(`mailto:${c.email}`, tr("Email"), false),
    c.phone && link(telHref(c.phone), esc(c.phone), false),
  ].filter(Boolean);
  return `<span>${tr("Not on hut-reservation.org")}${methods.length ? ` · ${tr("books {how}", { how: methods.join(", ") })}` : ""}</span>` +
    (links.length ? ` · ${links.join(" · ")}` : ` · ${tr("no contact known")}`);
}

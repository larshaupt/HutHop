// Offline use: registers the service worker (sw.js) and saves the tours in "My tours" for use without a connection.
// A kept tour is saved when it is opened or added: its JSON (with the height profiles), its huts' details and the
// topographic map along its route (swisstopo's tiles, and basemap.at's or OpenTopoMap's past the border), in a cache of
// its own that sw.js answers from offline. Removing the tour from "My tours" deletes it.
import { $, decodePolyline } from "./util.js";
import { LOCALE, t as tr } from "./i18n.js";  // t is a tour here
import { TOPO_TILES, mapTile } from "./map.js";
import { onYourToursChange, yourTours } from "./your-tours.js";

const PREFIX = "huthop-tour-";
export const MAX_TILES = 500; // map tiles per tour: about as many as a few minutes of browsing (the map servers' fair use)
const MIN_ZOOM = 8, MAX_ZOOM = 15;
const status = new Map(); // tour id → what the tour view says about its offline copy
const saving = new Set();

export function bindOffline() {
  navigator.serviceWorker?.register("/sw.js").catch((err) => console.warn("no offline use:", err));
  window.addEventListener("online", () => { $("#offlineNote").hidden = true; });
  onYourToursChange(prune);
}

/** After a fetch: if the service worker answered with saved data (no connection), say so and from when. */
export function noteSaved(res) {
  const saved = res.headers.get("x-huthop-saved");
  if (saved == null) return;
  const when = saved && new Date(saved).toLocaleString(LOCALE, { dateStyle: "medium", timeStyle: "short" });
  $("#offlineNote").textContent = when ? tr("Offline — showing saved data from {when}", { when }) : tr("Offline — showing saved data");
  $("#offlineNote").hidden = false;
}

export const offlineStatus = (tourId) => status.get(tourId) || "";

/** The map tiles along a tour's route (one tile around each point), zoom level by zoom level from MIN_ZOOM up while
 * they fit in MAX_TILES. Returns the tile URLs (TOPO_TILES) and the highest zoom level included. */
export function tourTiles(t) {
  const points = t.days.flatMap((d) => decodePolyline(d.geometry || ""));
  const urls = [];
  let zoom = 0;
  for (let z = MIN_ZOOM; z <= MAX_ZOOM; z++) {
    const n = 2 ** z, tiles = new Set();
    const tile = ([lon, lat]) => [(lon + 180) / 360 * n,
      (1 - Math.asinh(Math.tan(lat * Math.PI / 180)) / Math.PI) / 2 * n];
    const add = (x, y) => {
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
        tiles.add(TOPO_TILES.replace("{z}", z).replace("{x}", Math.floor(x) + dx).replace("{y}", Math.floor(y) + dy));
      }
    };
    points.forEach((p, i) => {
      const [x, y] = tile(p);
      add(x, y);
      if (!i) return;
      // fill gaps between route points further apart than half a tile
      const [px, py] = tile(points[i - 1]), steps = Math.ceil(2 * Math.max(Math.abs(x - px), Math.abs(y - py)));
      for (let s = 1; s < steps; s++) add(px + (x - px) * s / steps, py + (y - py) * s / steps);
    });
    if (urls.length + tiles.size > MAX_TILES) break;
    urls.push(...tiles);
    zoom = z;
  }
  return { urls, zoom };
}

/** Save a kept tour for offline use (again: it refreshes the tour and fetches only missing tiles). */
export async function saveTourOffline(t) {
  // not while showing saved data: that would store it again as new
  if (!("caches" in window) || !navigator.onLine || !$("#offlineNote").hidden || saving.has(t.id)) return;
  saving.add(t.id);
  const show = (text) => {
    status.set(t.id, text);
    const el = $("#offlineStatus");
    if (el?.dataset.tour === t.id) el.textContent = text;
  };
  try {
    const cache = await caches.open(PREFIX + t.id);
    await cache.put(`/api/tours/${encodeURIComponent(t.id)}`, new Response(JSON.stringify(t),
      { headers: { "content-type": "application/json", date: new Date().toUTCString() } }));
    for (const d of t.days.slice(0, -1)) {
      const res = await fetch(`/api/huts/${d.to.id}`);
      if (res.ok) await cache.put(`/api/huts/${d.to.id}`, res);
    }
    const { urls, zoom } = tourTiles(t);
    let done = 0, failed = 0, next = 0;
    // Each tile as the map puts it together, keeping what it fetches (only what this tour hasn't stored yet; a source
    // without that tile, such as basemap.at outside Austria, isn't kept). A few at a time.
    const get = async (url) => {
      const saved = await cache.match(url, { ignoreVary: true });
      if (saved) return saved;
      const res = await fetch(url);
      if (res.ok) await cache.put(url, res.clone());
      return res;
    };
    await Promise.all([0, 1, 2, 3].map(async () => {
      while (next < urls.length) {
        const [z, x, y] = urls[next++].split("/").slice(-3).map(Number);
        try {
          await mapTile("topo", z, x, y, get);
        } catch {
          failed++;
        }
        if (++done % 20 === 0) show(tr("Saving for offline use: map {done} of {total} tiles…", { done, total: urls.length }));
      }
    }));
    show(tr("Saved for offline use: the tour, its height profiles, huts and the map along the route up to zoom {zoom} ({tiles} tiles; at most {max} per tour).",
      { zoom, tiles: urls.length - failed, max: MAX_TILES }) + (failed ? ` ${tr("{n} tiles could not be loaded.", { n: failed })}` : ""));
  } catch (err) {
    console.warn("saving the tour for offline use failed:", err);
    show(tr("Could not save this tour for offline use."));
  } finally {
    saving.delete(t.id);
  }
}

/** Delete the offline copies of tours no longer in "My tours". */
async function prune() {
  if (!("caches" in window)) return;
  const kept = new Set(yourTours().map((e) => e.id));
  for (const name of await caches.keys()) {
    if (name.startsWith(PREFIX) && !kept.has(name.slice(PREFIX.length))) await caches.delete(name);
  }
}

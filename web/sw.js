// Service worker: HutHop without a connection, with what was loaded or saved before.
// - Our own files and API: network first (a deploy is never hidden behind the cache), the cache only as a fallback.
//   The app shell and the data it starts with are kept here; saved tours are stored by the page (js/offline.js).
// - swisstopo map tiles: from the cache when a saved tour stored them, else the network.
// Only 200 responses of our own origin with the expected content type are kept, so a Cloudflare Access login
// redirect is never cached.
const VERSION = 1; // bump when this file's caching changes: old shell caches are deleted when it activates
const SHELL = `huthop-shell-v${VERSION}`;
const FILES = [
  "/", "/style.css", "/manifest.json", "/icon-192.png", "/icon-512.png",
  "/vendor/maplibre-gl/maplibre-gl.js", "/vendor/maplibre-gl/maplibre-gl.css",
  "/js/beds.js", "/js/booking.js", "/js/connections.js", "/js/editor.js", "/js/favourites.js", "/js/feedback.js",
  "/js/huts.js", "/js/info.js", "/js/main.js", "/js/map.js", "/js/offline.js", "/js/settings.js", "/js/state.js",
  "/js/tour-detail.js", "/js/tour-editor.js", "/js/tours.js", "/js/util.js", "/js/weather.js", "/js/your-tours.js",
];
const API = ["/api/huts", "/api/osm-paths", "/api/info"]; // what the app needs to start, kept with the shell
const TYPES = { "": "text/html", html: "text/html", js: "javascript", css: "text/css", json: "application/json",
  png: "image/png" };
const MAP_HOSTS = ["https://wmts.geo.admin.ch/", "https://vectortiles.geo.admin.ch/fonts/"];

/** A response worth keeping for `path`: complete, ours, not redirected, of the type its extension promises. */
function keepable(res, path) {
  const type = TYPES[path.startsWith("/api/") ? "json" : path.split("/").pop().split(".").pop()];
  return res.status === 200 && res.type === "basic" && !res.redirected && !!type &&
    (res.headers.get("content-type") || "").includes(type);
}

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open(SHELL).then((cache) => Promise.all([...FILES, ...API].map((path) =>
    fetch(path, { cache: "no-cache" }).then((res) => keepable(res, path) && cache.put(path, res)).catch(() => {})))));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((names) => Promise.all(names.filter((n) => n.startsWith("huthop-shell-") && n !== SHELL).map((n) => caches.delete(n))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  if (MAP_HOSTS.some((host) => req.url.startsWith(host))) {
    event.respondWith(mapFile(event));
    return;
  }
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  const keep = !url.pathname.startsWith("/api/") || API.includes(url.pathname);
  event.respondWith(networkFirst(event, url.pathname, keep));
});

async function networkFirst(event, path, keep) {
  const req = event.request;
  try {
    const res = await fetch(req);
    if (keep && keepable(res, path)) {
      const copy = res.clone();
      event.waitUntil(caches.open(SHELL).then((cache) => cache.put(req, copy)));
    }
    return res;
  } catch (err) {
    // A saved tour is stored without the query (people, settings): any saved version beats none.
    const cached = await caches.match(req, { ignoreVary: true }) ||
      await caches.match(req, { ignoreSearch: true, ignoreVary: true });
    if (!cached) throw err;
    // Tell the page the answer is saved data, and from when (js/offline.js shows it)
    const headers = new Headers(cached.headers);
    headers.set("x-huthop-saved", cached.headers.get("date") || "");
    return new Response(cached.body, { status: cached.status, statusText: cached.statusText, headers });
  }
}

/** Map tiles (stored with saved tours) and label fonts (kept with the shell once loaded): cache first. */
async function mapFile(event) {
  const cached = await caches.match(event.request, { ignoreVary: true });
  if (cached) return cached;
  const res = await fetch(event.request);
  if (res.status === 200 && event.request.url.includes("/fonts/")) {
    const copy = res.clone();
    event.waitUntil(caches.open(SHELL).then((cache) => cache.put(event.request, copy)));
  }
  return res;
}

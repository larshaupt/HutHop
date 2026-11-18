// The MapLibre map: base styles, the trail overlay and shared map helpers.
import { $, CATEGORY, isMobile } from "./util.js";
import { state } from "./state.js";
import { settings } from "./settings.js";
import { t } from "./i18n.js";

const CH_BOUNDS = [[5.9, 45.8], [10.5, 47.85]];
const MAX_BOUNDS = [[4.5, 45.2], [17.8, 49.4]];  // Switzerland and all of Austria
const link = (href, text) => `<a href="${href}" target="_blank" rel="noopener">${text}</a>`;
const ATTRIBUTION = `${link("https://www.swisstopo.admin.ch", "© swisstopo")} · ${link("https://www.bkg.bund.de", "© BKG")} (2026) ` +
  `${link("https://www.govdata.de/dl-de/by-2-0", "dl-de/by-2-0")}, ` +
  `${link("https://sgx.geodatenzentrum.de/web_public/gdz/datenquellen/datenquellen_topplusopen.html", t("Data sources"))} · ` +
  `${link("https://waymarkedtrails.org", "© Waymarked Trails")} (CC BY-SA) · ` +
  `${t("Hut data")} ${link("https://www.sac-cas.ch", "© SAC-CAS")}, ${link("https://www.hut-reservation.org", "hut-reservation.org")} · ` +
  link("https://www.openstreetmap.org/copyright", "© OpenStreetMap");

// Each map comes in two parts, cut at the border of Switzerland and Liechtenstein (/api/border): swisstopo's inside,
// and outside BKG's TopPlusOpen, one calm map for all neighbouring countries (detailed in Germany and next to it). swisstopo's maps
// reach a few kilometres past the border, less the further you zoom in, and are white beyond: a cut there would jump
// with the zoom level. The parts are put together in the browser (mapTile), also for the tiles saved offline.
const swisstopo = (layer, ext) => ({ url: `https://wmts.geo.admin.ch/1.0.0/${layer}/default/current/3857/{z}/{x}/{y}.${ext}` });
const topPlus = (layer) => ({ url: `https://sgx.geodatenzentrum.de/wmts_topplus_open/tile/1.0.0/${layer}/default/WEBMERCATOR/{z}/{y}/{x}.png` });
const MAPS = {
  topo: { inside: swisstopo("ch.swisstopo.pixelkarte-farbe", "jpeg"), outside: [topPlus("web")] },
  // over swisstopo's light vector map, which shows little outside Switzerland: transparent inside the border
  light: { outside: [topPlus("web_light")] },
  // swissTLM3D's trails end at the border. Past it, our own trails (ownTrails) where we have them, else Waymarked
  // Trails, which only shows the ways in a hiking route in OpenStreetMap.
  trails: { inside: swisstopo("ch.swisstopo.swisstlm3d-wanderwege", "png"),
    outside: [{ url: "https://tile.waymarkedtrails.org/hiking/{z}/{x}/{y}.png", maxzoom: 18 }] },
};
// swisstopo's colours and line width for the trail categories
const TRAIL_COLORS = { 1: "#fdc200", 2: "#ff0000", 3: "#0000ff" };
const TRAIL_WIDTH = 2.5;
/** The topographic map's tiles (also saved with tours for offline use, js/offline.js). */
export const TOPO_TILES = "huthop://topo/{z}/{x}/{y}";
const STYLES = {
  light: "https://vectortiles.geo.admin.ch/styles/ch.swisstopo.lightbasemap.vt/style.json",
  topo: {
    version: 8,
    glyphs: "https://vectortiles.geo.admin.ch/fonts/{fontstack}/{range}.pbf",
    sources: {
      topo: {
        type: "raster", tileSize: 256, maxzoom: 18,
        tiles: [TOPO_TILES],
      },
    },
    layers: [{ id: "topo", type: "raster", source: "topo" }],
  },
};

// The border's rings in Web Mercator (0–1 across the world), or null without border data (then swisstopo everywhere)
const mercator = ([lon, lat]) => [(lon + 180) / 360, (1 - Math.asinh(Math.tan(lat * Math.PI / 180)) / Math.PI) / 2];
let border;
const borderRings = () => (border ??= fetch("/api/border").then((r) => r.json())
  .then((f) => f.geometry && (f.geometry.type === "Polygon" ? f.geometry.coordinates : f.geometry.coordinates.flat())
    .map((ring) => ring.map(mercator)))
  .catch(() => null));

/** Where a tile lies: "inside" or "outside" the border, or "both" (the border may cross it). */
function tileSide(rings, z, x, y) {
  const n = 2 ** z, [x0, y0, x1, y1] = [x / n, y / n, (x + 1) / n, (y + 1) / n];
  let crossings = 0;  // of a ray from the tile's centre to the west (even: outside)
  for (const ring of rings) {
    for (let i = 1; i < ring.length; i++) {
      const [ax, ay] = ring[i - 1], [bx, by] = ring[i];
      if (Math.max(ax, bx) >= x0 && Math.min(ax, bx) <= x1 && Math.max(ay, by) >= y0 && Math.min(ay, by) <= y1) return "both";
      const cy = (y0 + y1) / 2;
      if ((ay > cy) !== (by > cy) && ax + (cy - ay) / (by - ay) * (bx - ax) < x0) crossings++;
    }
  }
  return crossings % 2 ? "inside" : "outside";
}

/** Switzerland and Liechtenstein drawn on a tile (opaque inside the border), and its alpha per pixel. */
function borderMask(rings, z, x, y) {
  const canvas = new OffscreenCanvas(256, 256), ctx = canvas.getContext("2d", { willReadFrequently: true });
  const n = 2 ** z * 256;
  ctx.beginPath();
  for (const ring of rings) ring.forEach(([mx, my], i) => ctx[i ? "lineTo" : "moveTo"](mx * n - x * 256, my * n - y * 256));
  ctx.fill("evenodd");  // the holes are the enclaves (Büsingen, Campione)
  return { canvas, data: ctx.getImageData(0, 0, 256, 256).data };
}

const tileUrl = (source, z, x, y) => source.url.replace("{z}", z).replace("{x}", x).replace("{y}", y);

/** A source's tile, scaled up from its highest zoom level beyond it; null where it has none. */
async function tileImage(source, z, x, y, get) {
  const zoom = Math.min(z, source.maxzoom ?? z), over = 2 ** (z - zoom);
  const res = await get(tileUrl(source, zoom, Math.floor(x / over), Math.floor(y / over)));
  if (!res.ok || !(res.headers.get("content-type") || "").startsWith("image/")) return null;
  const bytes = await res.arrayBuffer();
  const image = await createImageBitmap(new Blob([bytes]));
  return { bytes, image, sx: (x % over) * 256 / over, sy: (y % over) * 256 / over, size: 256 / over };
}

/** Our trails on a tile (/api/trail-tiles, the network the planner walks abroad): their lines, and where they
 * replace the other sources: "all", null (nowhere: no lines either) or polygon rings. */
async function ownTrails(z, x, y, get) {
  const res = await get(`/api/trail-tiles/${z}/${x}/${y}`).catch(() => null);
  return res?.ok ? res.json() : { area: null, lines: [] };
}

const EXTENT = 4096;  // of the trail tiles' coordinates
const tracePath = (ctx, points) => {
  for (let i = 0; i < points.length; i += 2) ctx[i ? "lineTo" : "moveTo"](points[i] * 256 / EXTENT, points[i + 1] * 256 / EXTENT);
};

/** One tile of a map (MAPS) as image data: swisstopo's inside the border, the other sources' outside. `get` fetches
 * a URL (offline.js stores what it fetches). */
export async function mapTile(name, z, x, y, get = fetch) {
  const { inside } = MAPS[name];
  const rings = await borderRings();
  const side = rings ? tileSide(rings, z, x, y) : "inside";
  if (side === "inside") {
    if (!inside) return new ArrayBuffer(0);  // transparent
    const res = await get(tileUrl(inside, z, x, y));
    if (!res.ok) throw new Error(`${res.status} ${res.url}`);
    return res.arrayBuffer();
  }
  const own = name === "trails" && await ownTrails(z, x, y, get);
  const outside = own?.area === "all" ? [] : MAPS[name].outside;
  const mask = side === "both" && borderMask(rings, z, x, y);
  const canvas = new OffscreenCanvas(256, 256), ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.globalCompositeOperation = "destination-over";  // each next source only shows where the ones before are empty
  for (const [i, source] of outside.entries()) {
    const tile = await tileImage(source, z, x, y, get).catch(() => null);
    if (!tile) continue;
    ctx.filter = source.filter || "none";
    ctx.drawImage(tile.image, tile.sx, tile.sy, tile.size, tile.size, 0, 0, 256, 256);
    let gap = false;  // a pixel outside the border not covered yet (the trail overlay is transparent where it has none)
    const data = name !== "trails" && ctx.getImageData(0, 0, 256, 256).data;
    for (let p = 3; p < data.length && !gap; p += 4) gap = data[p] < 255 && !(mask && mask.data[p] === 255);
    if (gap) continue;
    // as it comes when this one source fills the whole tile
    if (!mask && !own?.area && !i && tile.size === 256 && !source.filter) return tile.bytes;
    break;
  }
  ctx.filter = "none";
  if (own?.area) {
    ctx.globalCompositeOperation = "destination-out";  // the other sources' trails, where ours replace them
    if (own.area !== "all") {
      ctx.beginPath();
      for (const ring of own.area) tracePath(ctx, ring);
      ctx.fill("evenodd");
    }
    ctx.globalCompositeOperation = "source-over";
    Object.assign(ctx, { lineWidth: TRAIL_WIDTH, lineCap: "round", lineJoin: "round" });
    for (const category of [1, 2, 3]) {  // the harder ones on top, as swisstopo's
      ctx.beginPath();
      for (const [c, ...points] of own.lines) if (c === category) tracePath(ctx, points);
      ctx.strokeStyle = TRAIL_COLORS[category];
      ctx.stroke();
    }
  }
  if (mask) {
    ctx.globalCompositeOperation = "destination-out";
    ctx.drawImage(mask.canvas, 0, 0);
    if (inside) {
      const tile = await tileImage(inside, z, x, y, get);
      ctx.globalCompositeOperation = "destination-over";
      if (tile) ctx.drawImage(tile.image, 0, 0);
    }
  }
  return (await canvas.convertToBlob({ type: "image/png" })).arrayBuffer();
}

// huthop://{map}/{z}/{x}/{y}: a tile of one of MAPS
maplibregl.addProtocol("huthop", async (params, abort) => {
  const [name, z, x, y] = params.url.slice("huthop://".length).split("/");
  return { data: await mapTile(name, +z, +x, +y, (url) => fetch(url, { signal: abort.signal })) };
});

/** The map instance (set by initMap). */
export let map;

/** Create the map. `onStyleLoad` runs after every (re)load of a base style, to add our own layers. */
export function initMap(onStyleLoad) {
  map = new maplibregl.Map({
    container: "map",
    style: STYLES[state.base],
    bounds: CH_BOUNDS,
    fitBoundsOptions: { padding: mapPadding() },
    maxBounds: MAX_BOUNDS,
    attributionControl: false,
    dragRotate: false,
    pitchWithRotate: false,
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: ATTRIBUTION }), "bottom-right");
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: false } }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
  map.on("style.load", () => {
    if (!map.getSource("topo")) {  // the light map: other maps over it outside Switzerland
      map.addSource("abroad", { type: "raster", tiles: ["huthop://light/{z}/{x}/{y}"], tileSize: 256, maxzoom: 18 });
      map.addLayer({ id: "abroad", type: "raster", source: "abroad" });
    }
    onStyleLoad();
  });
  bindOsmPaths();
}

/** Hover on an unofficial path: what it is and its grade (handlers survive style reloads). */
function bindOsmPaths() {
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
  map.on("mousemove", "osm-paths", (e) => {
    const grade = e.features[0].properties.grade;
    map.getCanvas().style.cursor = "help";
    popup.setLngLat(e.lngLat).setHTML(`${t("Unofficial path")}${grade ? ` · ${grade}` : ""}<small>${t("OpenStreetMap, not signposted")}</small>`)
      .addTo(map);
  });
  map.on("mouseleave", "osm-paths", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });
}

/** All of Switzerland, as at the start. */
export function showAll() {
  map.fitBounds(CH_BOUNDS, { padding: mapPadding(), duration: 700 });
}

export function setBaseStyle(name) {
  map.setStyle(STYLES[name], { diff: false });  // a full reload re-runs onStyleLoad, so our layers come back
}

export function addTrailLayer() {
  map.addSource("trails", { type: "raster", tiles: ["huthop://trails/{z}/{x}/{y}"], tileSize: 256, maxzoom: 18 });
  map.addLayer({
    id: "trails", type: "raster", source: "trails",
    paint: { "raster-opacity": ["interpolate", ["linear"], ["zoom"], 7, 0.45, 10, 0.9] },
    layout: { visibility: state.trails ? "visible" : "none" },
  });
  // Unofficial OSM paths to huts off the signposted network, with the trails when the settings allow them
  map.addSource("osm-paths", { type: "geojson", data: "/api/osm-paths" });
  map.addLayer({
    id: "osm-paths", type: "line", source: "osm-paths",
    paint: { "line-color": CATEGORY[4].color, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.5, 13, 3],
      "line-dasharray": [2, 1.5] },
    layout: { visibility: osmVisible(state.trails) ? "visible" : "none", "line-cap": "butt" },
  });
}

const osmVisible = (trails) => trails && settings().limit >= 4;

/** The trail overlay on or off; also call it when the settings change (unofficial paths follow the trail limit). */
export function setTrailsVisible(visible) {
  if (map.getLayer("trails")) map.setLayoutProperty("trails", "visibility", visible ? "visible" : "none");
  if (map.getLayer("osm-paths")) map.setLayoutProperty("osm-paths", "visibility", osmVisible(visible) ? "visible" : "none");
}

/** Space the panel covers, so fitted content stays visible. */
export function mapPadding() {
  const top = $("#topbar").offsetHeight + 24;  // the search and filter bar over the map
  if (isMobile()) return { top, bottom: window.innerHeight * 0.55, left: 20, right: 20 };
  return { top, bottom: 40, left: $("#panel").offsetWidth + 40, right: 40 };
}

export function fitCoords(coords, maxZoom = 13) {
  if (!coords.length) return;
  const bounds = coords.reduce((b, c) => b.extend(c), new maplibregl.LngLatBounds(coords[0], coords[0]));
  map.fitBounds(bounds, { padding: mapPadding(), maxZoom, duration: 700 });
}

/** Map bounds of the area not covered by the panel, as [west, south, east, north]. */
export function visibleBox() {
  const canvas = map.getCanvas().getBoundingClientRect();
  const panel = $("#panel").getBoundingClientRect();
  const left = isMobile() ? 0 : panel.right - canvas.left;
  const bottom = isMobile() ? panel.top - canvas.top : canvas.height;
  const sw = map.unproject([left, bottom]);
  const ne = map.unproject([canvas.width, 0]);
  return [sw.lng, sw.lat, ne.lng, ne.lat];
}

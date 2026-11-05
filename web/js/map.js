// The MapLibre map: base styles, the trail overlay and shared map helpers.
import { $, CATEGORY, isMobile } from "./util.js";
import { state } from "./state.js";
import { settings } from "./settings.js";

const CH_BOUNDS = [[5.9, 45.8], [10.5, 47.85]];
const ATTRIBUTION = '<a href="https://www.swisstopo.admin.ch" target="_blank" rel="noopener">© swisstopo</a> · ' +
  'Hut data <a href="https://www.sac-cas.ch" target="_blank" rel="noopener">© SAC-CAS</a> · ' +
  'Unofficial paths <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap</a>';
const TRAILS_TILES = "https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swisstlm3d-wanderwege/default/current/3857/{z}/{x}/{y}.png";
const STYLES = {
  light: "https://vectortiles.geo.admin.ch/styles/ch.swisstopo.lightbasemap.vt/style.json",
  topo: {
    version: 8,
    glyphs: "https://vectortiles.geo.admin.ch/fonts/{fontstack}/{range}.pbf",
    sources: {
      topo: {
        type: "raster", tileSize: 256, maxzoom: 18,
        tiles: ["https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg"],
      },
    },
    layers: [{ id: "topo", type: "raster", source: "topo" }],
  },
};

/** The map instance (set by initMap). */
export let map;

/** Create the map. `onStyleLoad` runs after every (re)load of a base style, to add our own layers. */
export function initMap(onStyleLoad) {
  map = new maplibregl.Map({
    container: "map",
    style: STYLES[state.base],
    bounds: CH_BOUNDS,
    fitBoundsOptions: { padding: mapPadding() },
    maxBounds: [[4.5, 45.2], [11.9, 48.3]],
    attributionControl: false,
    dragRotate: false,
    pitchWithRotate: false,
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: ATTRIBUTION }), "bottom-right");
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: false } }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
  map.on("style.load", onStyleLoad);
  bindOsmPaths();
}

/** Hover on an unofficial path: what it is and its grade (handlers survive style reloads). */
function bindOsmPaths() {
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
  map.on("mousemove", "osm-paths", (e) => {
    const grade = e.features[0].properties.grade;
    map.getCanvas().style.cursor = "help";
    popup.setLngLat(e.lngLat).setHTML(`Unofficial path${grade ? ` · ${grade}` : ""}<small>OpenStreetMap, not signposted</small>`)
      .addTo(map);
  });
  map.on("mouseleave", "osm-paths", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });
}

export function setBaseStyle(name) {
  map.setStyle(STYLES[name], { diff: false });  // a full reload re-runs onStyleLoad, so our layers come back
}

export function addTrailLayer() {
  map.addSource("trails", { type: "raster", tiles: [TRAILS_TILES], tileSize: 256, maxzoom: 18 });
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
  if (isMobile()) return { top: 60, bottom: window.innerHeight * 0.55, left: 20, right: 20 };
  return { top: 60, bottom: 40, left: $("#panel").offsetWidth + 40, right: 40 };
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

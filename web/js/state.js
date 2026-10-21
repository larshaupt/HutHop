// App state, mirrored in the URL hash so every view can be shared and bookmarked.

export const ALT_MAX = 4600;

export const DEFAULTS = {
  // hut browser
  month: 0, openAs: "any", hb: false, kitchen: false, bookable: false,
  altMin: 0, altMax: ALT_MAX, minBeds: 0, operator: "all", q: "", sort: "name",
  hut: 0, base: "light", trails: false, limit: 2,
  // tour planner
  view: "huts", tDate: "", tNights: 2, tMin: 3, tMax: 6, tCat: "any", tStart: 0, tStartName: "", tLoop: false,
  tUnknown: true, tBox: "", tour: 0, tPeople: 2, tAvail: true, tSort: "best", tLifts: false,
};

const HASH_KEYS = {
  month: "m", openAs: "open", hb: "hb", kitchen: "kitchen", bookable: "book", altMin: "amin", altMax: "amax",
  minBeds: "beds", operator: "type", q: "q", sort: "sort", hut: "hut", base: "map", trails: "trails", limit: "tl",
  view: "v", tDate: "td", tNights: "tn", tMin: "tmin", tMax: "tmax", tCat: "tc", tStart: "ts", tStartName: "tsn",
  tLoop: "loop", tUnknown: "unk", tBox: "tb", tour: "tour", tPeople: "ppl", tAvail: "avail", tSort: "tsort",
  tLifts: "nolifts",
};

/** The one state object. Modules read it directly and change it only through setState(). */
export const state = readHash();

export function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const s = { ...DEFAULTS };
  for (const [key, short] of Object.entries(HASH_KEYS)) {
    if (!params.has(short)) continue;
    const raw = params.get(short);
    const def = DEFAULTS[key];
    s[key] = typeof def === "boolean" ? raw === "1" : typeof def === "number" ? Number(raw) || 0 : raw;
  }
  return s;
}

function writeHash() {
  const params = new URLSearchParams();
  for (const [key, short] of Object.entries(HASH_KEYS)) {
    const v = state[key];
    if (v === DEFAULTS[key]) continue;
    params.set(short, typeof v === "boolean" ? (v ? "1" : "0") : v);
  }
  const hash = params.toString();
  history.replaceState(null, "", hash ? `#${hash}` : location.pathname);
}

export function setState(patch) {
  Object.assign(state, patch);
  writeHash();
}

/** Replace the whole state from the URL (back/forward, edited links). */
export function reloadFromHash() {
  Object.assign(state, readHash());
}

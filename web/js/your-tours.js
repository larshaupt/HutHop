// "My tours": tours kept in this browser (local storage, per device), each entry as its tour id plus a label for
// the list. Per device by design (no accounts or sync); offline.js also saves them for use without a connection.
// Storage can be unavailable (private windows): then the list is empty.
const KEY = "huthop.yourTours";
const listeners = [];

function read() {
  try {
    const list = JSON.parse(localStorage.getItem(KEY) || "[]");
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

function write(list) {
  try {
    localStorage.setItem(KEY, JSON.stringify(list));
  } catch {
    // storage full or blocked: the change lives only until the page is reloaded
  }
  listeners.forEach((fn) => fn());
}

/** Label of a tour for the list (kept with the entry, so the list needs no request per tour). */
function label(t) {
  return {
    id: t.id, title: `${t.days[0].from.name} → ${t.days[t.days.length - 1].to.name}`,
    huts: t.days.slice(0, -1).map((d) => d.to.name), date: t.days[0].date, nights: t.nights,
  };
}

export const yourTours = () => read();
export const findEntry = (tourId) => read().find((e) => e.id === tourId);
export const onYourToursChange = (fn) => listeners.push(fn);

/** Keep a tour (once; an already kept tour returns its entry). */
export function keepTour(t) {
  const list = read();
  const existing = list.find((e) => e.id === t.id);
  if (existing) return existing;
  const entry = { key: `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`, ...label(t),
    added: new Date().toISOString() };
  write([entry, ...list]);
  return entry;
}

/** Point an entry at an edited version of its tour. */
export function updateEntry(key, t) {
  write(read().map((e) => (e.key === key ? { ...e, ...label(t) } : e)));
}

export function removeEntry(key) {
  write(read().filter((e) => e.key !== key));
}

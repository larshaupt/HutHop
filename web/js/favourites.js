// Favourite huts, kept in this browser (local storage, per device) like "My tours". Storage can be unavailable
// (private windows): then there are no favourites.
const KEY = "huthop.favourites";

function read() {
  try {
    const ids = JSON.parse(localStorage.getItem(KEY) || "[]");
    return new Set(Array.isArray(ids) ? ids : []);
  } catch {
    return new Set();
  }
}

export const isFavourite = (id) => read().has(id);

/** Add or remove a hut; returns whether it is a favourite now. */
export function toggleFavourite(id) {
  const ids = read();
  const now = !ids.delete(id);
  if (now) ids.add(id);
  try {
    localStorage.setItem(KEY, JSON.stringify([...ids]));
  } catch {
    // storage full or blocked: the change lives only until the page is reloaded
  }
  return now;
}

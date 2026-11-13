// Translations. t("English text", { n: 3 }) gives the text in the chosen language: the English wording is the key,
// so English needs a dictionary only for plurals, and a text without a translation shows in English. The language
// is the one chosen in the header (kept with the settings), else the browser's; changing it reloads the page.
import en from "./i18n/en.js";
import de from "./i18n/de.js";

export const LANGUAGES = { en: "EN", de: "DE" };
const DICTS = { en, de };

function chosen() {
  try {
    const saved = JSON.parse(localStorage.getItem("huthop.settings") || "{}").lang;  // see settings.js
    if (saved in LANGUAGES) return saved;
  } catch {
    // storage blocked: the browser's language
  }
  // shortcut: French and Italian browsers get German until there are dictionaries for them
  const lang = navigator.languages?.map((l) => l.slice(0, 2)).find((l) => ["en", "de", "fr", "it"].includes(l));
  return !lang || lang === "en" ? "en" : "de";
}

export const LANG = chosen();
/** For Intl and toLocale…String: Swiss German, or British English (day before month, as in Switzerland). */
export const LOCALE = LANG === "de" ? "de-CH" : "en-GB";
const plurals = new Intl.PluralRules(LOCALE);
const lookup = (dict, key) => (Object.hasOwn(dict, key) ? dict[key] : undefined);

/** The text for `key` in the chosen language, with {name} replaced by params.name (inserted as is: escape HTML
 * before). A plural entry ({ one, other }) is chosen by params.n. Unknown keys come back unchanged, so text from
 * the API passes through when there is no translation for it. */
export function t(key, params = {}) {
  let text = lookup(DICTS[LANG], key) ?? lookup(en, key) ?? key;
  if (typeof text === "object") text = text[plurals.select(params.n)] ?? text.other;
  return text.replace(/\{(\w+)\}/g, (m, name) => (name in params ? params[name] : m));
}

/** The static text of index.html (text, aria-label, title, placeholder), translated by its English wording. */
export function translatePage() {
  document.documentElement.lang = LANG;
  if (LANG === "en") return;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node; (node = walker.nextNode());) {
    const text = node.nodeValue.trim();
    if (text && lookup(DICTS[LANG], text)) node.nodeValue = node.nodeValue.replace(text, t(text));
  }
  for (const el of document.querySelectorAll("[aria-label], [title], [placeholder], meta[name=description]")) {
    for (const attr of ["aria-label", "title", "placeholder", "content"]) {
      const value = el.getAttribute(attr);
      if (value && lookup(DICTS[LANG], value)) el.setAttribute(attr, t(value));
    }
  }
}

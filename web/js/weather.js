// Short-term weather at a hut (MeteoSwiss forecast via /api/huts/{id}/weather, about 5 days): in the hut details,
// and per day in the tour view.
import { esc } from "./util.js";

// WMO weather codes (as Open-Meteo returns them) → symbol and words
const CODES = [
  [0, "☀️", "Clear"], [1, "🌤️", "Mostly clear"], [2, "⛅", "Partly cloudy"], [3, "☁️", "Overcast"],
  [48, "🌫️", "Fog"], [57, "🌦️", "Drizzle"], [67, "🌧️", "Rain"], [77, "🌨️", "Snow"], [82, "🌧️", "Showers"],
  [86, "🌨️", "Snow showers"], [99, "⛈️", "Thunderstorms"],
];
const DAY_FMT = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric" });
const forecasts = new Map(); // hut id → promise of { days, error }, kept for the page's life (the server caches an hour)

const describe = (code) => CODES.find(([max]) => code <= max) || CODES[CODES.length - 1];

export function hutForecast(id) {
  if (!forecasts.has(id)) {
    forecasts.set(id, fetch(`/api/huts/${id}/weather`).then((r) => (r.ok ? r.json() : { days: [], error: true }))
      .catch(() => ({ days: [], error: true })));
  }
  return forecasts.get(id);
}

/** One day's forecast in a line, e.g. "⛅ Partly cloudy · 2–9 °C · 1.2 mm · wind 25 km/h". */
export function weatherLine(d) {
  const [, icon, words] = describe(d.weather_code);
  const rain = d.precipitation_sum >= 0.1 ? ` · ${d.precipitation_sum.toFixed(1)} mm` : "";
  return `${icon} ${words} · ${Math.round(d.temperature_2m_min)}–${Math.round(d.temperature_2m_max)} °C${rain}` +
    ` · wind ${Math.round(d.wind_speed_10m_max)} km/h`;
}

/** The hut's forecast as a row of days into `el` (if it is still on the page). */
export async function loadWeather(id, el) {
  const data = await hutForecast(id);
  if (!el.isConnected) return;
  if (data.error || !data.days.length) {
    el.innerHTML = `<p class="hint">No forecast available right now.</p>`;
    return;
  }
  el.innerHTML = `<div class="forecast">${data.days.map((d) => {
    const [, icon, words] = describe(d.weather_code);
    return `<div title="${esc(weatherLine(d))}"><small>${DAY_FMT.format(new Date(`${d.date}T12:00:00`))}</small>
      <span class="wx-icon" aria-label="${words}">${icon}</span><b>${Math.round(d.temperature_2m_max)}°</b>
      <small>${Math.round(d.temperature_2m_min)}°${d.precipitation_sum >= 0.5 ? ` · ${Math.round(d.precipitation_sum)} mm` : ""}</small></div>`;
  }).join("")}</div>
    <p class="hint">MeteoSwiss forecast at the hut's altitude (via Open-Meteo). Mountain weather changes fast: check
      <a href="https://www.meteoswiss.admin.ch" target="_blank" rel="noopener">MeteoSwiss</a> before you go.</p>`;
}

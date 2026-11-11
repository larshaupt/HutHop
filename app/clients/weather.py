"""Short-term weather: MeteoSwiss's ICON-CH2 forecast (about 5 days) through Open-Meteo, for a point at a given
altitude (the model is corrected to it, which matters in the mountains). Free for non-commercial use."""

import time

import httpx

from app.clients import USER_AGENT

URL = "https://api.open-meteo.com/v1/forecast"
DAILY = ("weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum", "wind_speed_10m_max")
TIMEOUT_S = 10
CACHE_TTL_S = 3600  # the model runs every few hours
# shortcut: an in-process cache (lost on restart, one per worker); move it into the database if that ever matters
_cache: dict[tuple, tuple[float, list[dict]]] = {}


def parse_forecast(data: dict) -> list[dict]:
    """One entry per day with a forecast (days beyond the model's range come back empty and are left out)."""
    daily = data["daily"]
    days = [dict(zip(("date", *DAILY), values)) for values in zip(daily["time"], *(daily[k] for k in DAILY))]
    return [d for d in days if d["weather_code"] is not None]


def forecast(lat: float, lon: float, altitude_m: int | None) -> list[dict]:
    """Daily forecast from today, cached for an hour per point. Network and HTTP errors are raised."""
    key = (round(lat, 4), round(lon, 4), altitude_m)
    if (hit := _cache.get(key)) and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    params = {"latitude": lat, "longitude": lon, "models": "meteoswiss_icon_ch2", "daily": ",".join(DAILY),
              "timezone": "Europe/Zurich", "forecast_days": 6}
    if altitude_m:
        params["elevation"] = altitude_m
    r = httpx.get(URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
    r.raise_for_status()
    days = parse_forecast(r.json())
    _cache[key] = (time.time(), days)
    return days

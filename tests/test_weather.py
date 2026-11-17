"""Parsing of the Open-Meteo forecast and the fallback model outside ICON-CH2's area (no network)."""

import httpx

from app.clients import weather
from app.clients.weather import parse_forecast


def test_parse_forecast_drops_days_beyond_the_model():
    days = parse_forecast({"daily": {
        "time": ["2026-10-27", "2026-10-28"], "weather_code": [3, None], "temperature_2m_max": [10.6, None],
        "temperature_2m_min": [4.4, None], "precipitation_sum": [0.0, None], "wind_speed_10m_max": [8.0, None]}})
    assert days == [{"date": "2026-10-27", "weather_code": 3, "temperature_2m_max": 10.6, "temperature_2m_min": 4.4,
                     "precipitation_sum": 0.0, "wind_speed_10m_max": 8.0}]


DAY = {"time": ["2026-11-17"], "weather_code": [61], "temperature_2m_max": [6.3], "temperature_2m_min": [1.6],
       "precipitation_sum": [11.5], "wind_speed_10m_max": [11.7]}


def fake_open_meteo(covered: bool, monkeypatch) -> list[str]:
    """Answer like Open-Meteo for a point inside or outside ICON-CH2's area; returns the models asked for."""
    asked = []

    def get(url, params, **kwargs):
        asked.append(params["models"])
        request = httpx.Request("GET", url)
        if params["models"] == "meteoswiss_icon_ch2" and not covered:  # what Open-Meteo answers east of ~16.8°E
            return httpx.Response(400, json={"error": True, "reason": "No data is available for this location"},
                                  request=request)
        return httpx.Response(200, json={"daily": DAY}, request=request)

    monkeypatch.setattr(weather.httpx, "get", get)
    monkeypatch.setattr(weather, "_cache", {})
    return asked


def test_forecast_uses_meteoswiss_where_it_has_data(monkeypatch):
    asked = fake_open_meteo(True, monkeypatch)
    result = weather.forecast(47.1, 11.3, 2000)  # Stubai
    assert asked == ["meteoswiss_icon_ch2"]
    assert result["source"] == "meteoswiss" and result["days"][0]["weather_code"] == 61


def test_forecast_falls_back_to_dwd_outside_icon_ch2(monkeypatch):
    asked = fake_open_meteo(False, monkeypatch)
    result = weather.forecast(47.95, 16.84, 120)  # Neusiedl am See
    assert asked == ["meteoswiss_icon_ch2", "icon_seamless"]
    assert result["source"] == "dwd" and result["days"][0]["date"] == "2026-11-17"

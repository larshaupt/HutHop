"""Parsing of the Open-Meteo forecast (no network)."""

from app.clients.weather import parse_forecast


def test_parse_forecast_drops_days_beyond_the_model():
    days = parse_forecast({"daily": {
        "time": ["2026-10-27", "2026-10-28"], "weather_code": [3, None], "temperature_2m_max": [10.6, None],
        "temperature_2m_min": [4.4, None], "precipitation_sum": [0.0, None], "wind_speed_10m_max": [8.0, None]}})
    assert days == [{"date": "2026-10-27", "weather_code": 3, "temperature_2m_max": 10.6, "temperature_2m_min": 4.4,
                     "precipitation_sum": 0.0, "wind_speed_10m_max": 8.0}]

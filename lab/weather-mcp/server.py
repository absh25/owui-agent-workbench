"""Small Streamable-HTTP weather MCP backed by Open-Meteo (no API key)."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


mcp = FastMCP(
    "Local Weather MCP",
    instructions=(
        "Get current weather and multi-day forecasts for cities worldwide. "
        "Resolve ambiguous locations by including province/state and country."
    ),
    host="0.0.0.0",
    port=8000,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


WEATHER_CODES = {
    0: "晴朗",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴天",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "中等毛毛雨",
    55: "强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "中等阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


async def _resolve(location: str) -> dict[str, Any]:
    location = location.strip()
    if not location:
        raise ValueError("location cannot be empty")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "zh", "format": "json"},
        )
        response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        raise ValueError(f"找不到地点：{location}")
    return results[0]


def _place_name(place: dict[str, Any]) -> str:
    parts = [place.get("name"), place.get("admin1"), place.get("country")]
    return ", ".join(str(part) for part in parts if part)


@mcp.tool()
async def get_current_weather(location: str) -> dict[str, Any]:
    """查询城市或地区的实时天气。location 建议包含城市、省/州和国家。"""
    place = await _resolve(location)
    fields = (
        "temperature_2m,apparent_temperature,relative_humidity_2m,"
        "precipitation,weather_code,wind_speed_10m,wind_direction_10m"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": fields,
                "timezone": "auto",
            },
        )
        response.raise_for_status()
    data = response.json()
    current = data.get("current") or {}
    code = current.get("weather_code")
    return {
        "location": _place_name(place),
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "timezone": data.get("timezone"),
        "observed_at": current.get("time"),
        "condition": WEATHER_CODES.get(code, f"未知天气代码 {code}"),
        "temperature_c": current.get("temperature_2m"),
        "apparent_temperature_c": current.get("apparent_temperature"),
        "relative_humidity_percent": current.get("relative_humidity_2m"),
        "precipitation_mm": current.get("precipitation"),
        "wind_speed_kmh": current.get("wind_speed_10m"),
        "wind_direction_degrees": current.get("wind_direction_10m"),
        "source": "Open-Meteo",
    }


@mcp.tool()
async def get_weather_forecast(location: str, days: int = 3) -> dict[str, Any]:
    """查询未来天气预报。days 为 1 到 7 天，location 建议包含城市和国家。"""
    place = await _resolve(location)
    days = max(1, min(int(days), 7))
    daily_fields = (
        "weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,precipitation_sum,wind_speed_10m_max"
    )
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "daily": daily_fields,
                "forecast_days": days,
                "timezone": "auto",
            },
        )
        response.raise_for_status()
    data = response.json()
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    forecast = []
    for index, date in enumerate(dates):
        code = (daily.get("weather_code") or [None] * len(dates))[index]
        forecast.append(
            {
                "date": date,
                "condition": WEATHER_CODES.get(code, f"未知天气代码 {code}"),
                "temperature_max_c": daily["temperature_2m_max"][index],
                "temperature_min_c": daily["temperature_2m_min"][index],
                "precipitation_probability_percent": daily[
                    "precipitation_probability_max"
                ][index],
                "precipitation_mm": daily["precipitation_sum"][index],
                "wind_speed_max_kmh": daily["wind_speed_10m_max"][index],
            }
        )
    return {
        "location": _place_name(place),
        "timezone": data.get("timezone"),
        "forecast": forecast,
        "source": "Open-Meteo",
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

"""
Данные для веб-панелей quest-ai (static/files — панель участника,
static/files-energy — панель энергетика) из телеметрии сервера купола.

Форматы совпадают с демо-генераторами панелей (mockTelemetry в
files/js/telemetry.js и mockTick в files-energy/js/energy.js).

Прогнозы солнца и ветра — простая оценка: суточная кривая солнца
(восход 6:00, закат 20:00, как в модели окружения купола) с учётом текущей
облачности; ветер — плавный возврат текущей скорости к среднему.
"""
from __future__ import annotations

import math
from typing import Any

SOLAR_RATED_KW = 1.2      # суммарная мощность солнечных панелей купола
MEAN_WIND_MS = 4.5
SUNRISE_H, SUNSET_H = 6.0, 20.0

PRINTER_STATUS = {"PRINTING": "printing", "PAUSED": "paused", "ERROR": "error"}
CNC_STATUS = {"RUNNING": "milling", "PAUSED": "paused", "ALARM": "error"}


def _f(value: Any, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def sun_factor(hour: float) -> float:
    hour %= 24.0
    if not SUNRISE_H < hour < SUNSET_H:
        return 0.0
    return math.sin(math.pi * (hour - SUNRISE_H) / (SUNSET_H - SUNRISE_H))


def _cloud_cover(telemetry: dict[str, Any]) -> float:
    env = telemetry.get("environment") or {}
    return _f(env.get("outdoor", {}).get("cloud_cover"), 0.3)


def _game_hour(telemetry: dict[str, Any]) -> float:
    return _f((telemetry.get("game_time") or {}).get("hour"), 12.0)


def _machine(node: dict[str, Any], status_map: dict[str, str], left_key: str) -> dict[str, Any]:
    status = status_map.get(node.get("state"), "idle")
    return {
        "status": status,
        "progress": round(_f(node.get("progress_pct")), 1) if status != "idle" else 0.0,
        "remainMin": int(_f(node.get(left_key)) // 60),
    }


def participant_telemetry(telemetry: dict[str, Any]) -> dict[str, Any]:
    """Формат панели участника (files/js/telemetry.js)."""
    nodes = telemetry.get("nodes", {})
    climate = nodes.get("climate_sensor_01", {})
    dizel = nodes.get("dizel_1", {})
    hour = _game_hour(telemetry)
    cloud_k = 1.0 - 0.75 * _cloud_cover(telemetry) ** 1.5
    wind_now = _f(nodes.get("weather_station_01", {}).get("wind_speed_ms"), MEAN_WIND_MS)
    lines = nodes.get("smart_panel_01", {}).get("lines", [])
    return {
        "solarKw": _f(nodes.get("solar_panels_01", {}).get("power_w")) / 1000.0,
        "windKw": _f(nodes.get("wind_turbine_01", {}).get("power_w")) / 1000.0,
        "lines": [_f(l.get("power_w")) / 1000.0 for l in lines],
        "temp": _f(climate.get("temperature_c")),
        "hum": _f(climate.get("humidity_pct")),
        "co2": _f(climate.get("co2_ppm")),
        "printer": _machine(nodes.get("printer_3d_01", {}), PRINTER_STATUS, "print_time_left_s"),
        "cnc": _machine(nodes.get("cnc_01", {}), CNC_STATUS, "job_time_left_s"),
        "diesel": {
            "on": dizel.get("state") == "ON",
            "kw": _f(dizel.get("power_w")) / 1000.0,
            "fuel": _f(nodes.get("fuel_tank_01", {}).get("level_pct")),
        },
        # прогноз на 12 часов вперёд, начиная со следующего игрового часа
        "forecastStartHour": (int(hour) + 1) % 24,
        "fSun": [round(100 * sun_factor(int(hour) + 1 + i) * cloud_k) for i in range(12)],
        "fWind": [round(MEAN_WIND_MS + (wind_now - MEAN_WIND_MS) * math.exp(-(i + 1) / 3.0), 1) for i in range(12)],
        "gameTime": telemetry.get("game_time"),
    }


def energy_telemetry(telemetry: dict[str, Any], horizon_min: int = 720, step_min: int = 30) -> dict[str, Any]:
    """Формат панели энергетика (files-energy/js/energy.js)."""
    nodes = telemetry.get("nodes", {})
    game = telemetry.get("game_time") or {}
    minute = _f(game.get("day")) * 1440 + _game_hour(telemetry) * 60
    lines = nodes.get("smart_panel_01", {}).get("lines", [])
    gen_w = (_f(nodes.get("solar_panels_01", {}).get("power_w"))
             + _f(nodes.get("wind_turbine_01", {}).get("power_w"))
             + _f(nodes.get("dizel_1", {}).get("power_w")))

    cloud_k = 1.0 - 0.75 * _cloud_cover(telemetry) ** 1.5
    steps = horizon_min // step_min
    f_t, f_mean, f_sd = [], [], []
    for i in range(steps + 1):
        t = minute + i * step_min
        base = sun_factor(t / 60.0)
        f_t.append(t)
        f_mean.append(round(SOLAR_RATED_KW * base * cloud_k, 3))
        f_sd.append(round((0.12 + 0.3 * i / steps) * SOLAR_RATED_KW * (0.25 + 0.75 * base), 3))

    return {
        "t": minute,
        "gen": round(gen_w / 1000.0, 3),
        "cons": round(_f(nodes.get("smart_panel_01", {}).get("total_power_w")) / 1000.0, 3),
        "per": [round(_f(l.get("power_w")) / 1000.0, 3) for l in lines],
        "on": [l.get("state") == "ON" for l in lines],
        "states": [l.get("state") for l in lines],
        "names": [l.get("name") for l in lines],
        "forecast": {"t": f_t, "mean": f_mean, "sd": f_sd},
    }

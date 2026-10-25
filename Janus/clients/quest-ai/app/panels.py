"""
Данные для панели участника quest-ai (static/index.html) из телеметрии сервера купола.

Формат совпадает с демо-генератором панели (mockTelemetry в js/telemetry.js).

Прогнозы солнца и ветра — простая оценка: суточная кривая солнца
(восход 6:00, закат 20:00, как в модели окружения купола) с учётом текущей
облачности; ветер — плавный возврат текущей скорости к среднему.
"""
from __future__ import annotations

import math
from typing import Any

MEAN_WIND_MS = 4.5
SUNRISE_H, SUNSET_H = 6.0, 20.0

PRINTER_STATUS = {"PRINTING": "printing", "PAUSED": "paused", "ERROR": "error", "COMPLETED": "completed"}
CNC_STATUS = {"RUNNING": "milling", "PAUSED": "paused", "ALARM": "error", "HOMING": "busy", "BUSY": "busy"}
# Короткие подписи линий щитка (полные названия — в telemetry lines[].name)
LINE_SHORT = {1: "Мастерская + вытяжка", 2: "Теплица ФабЛаб", 3: "3D-принтер", 4: "ЧПУ-фрезер", 5: "Теплица Химлаб",
              6: "Освещение", 7: "Аварийный свет", 8: "Резерв"}


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
        "progress": round(_f(node.get("progress_pct")), 1) if status not in ("idle", "busy") else 0.0,
        "remainMin": int(_f(node.get(left_key)) // 60),
        "file": node.get("file_name"),
        # удержание сервером (пауза автоматики): None | WARNING | ALARM и код причины
        "hold": node.get("hold_level"),
        "holdReason": node.get("hold_reason"),
        "online": node.get("online", True),
    }


def participant_telemetry(telemetry: dict[str, Any]) -> dict[str, Any]:
    """Формат панели участника (js/telemetry.js)."""
    nodes = telemetry.get("nodes", {})
    climate = nodes.get("climate_sensor_01", {})
    dizel = nodes.get("dizel_1", {})
    hour = _game_hour(telemetry)
    cloud_k = 1.0 - 0.75 * _cloud_cover(telemetry) ** 1.5
    wind_now = _f(nodes.get("weather_station_01", {}).get("wind_speed_ms"), MEAN_WIND_MS)
    lines = nodes.get("smart_panel_01", {}).get("lines", [])
    inverter = nodes.get("solar_inverter_01", {})
    printer = nodes.get("printer_3d_01", {})
    wind = nodes.get("wind_turbine_01", {})
    return {
        "solarKw": _f(nodes.get("solar_panels_01", {}).get("power_w")) / 1000.0,
        "windKw": _f(wind.get("power_w")) / 1000.0,
        "windState": wind.get("state"),
        "lines": [_f(l.get("power_w")) / 1000.0 for l in lines],
        "lineNames": [LINE_SHORT.get(l.get("line"), l.get("name")) for l in lines],
        "lineStates": [l.get("state") for l in lines],
        "battery": {
            "soc": inverter.get("battery_soc_pct"),
            "mode": inverter.get("mode"),
            "error": inverter.get("error_code"),
            "grid": inverter.get("grid_state"),
        },
        "temp": _f(climate.get("temperature_c")),
        "hum": _f(climate.get("humidity_pct")),
        "co2": _f(climate.get("co2_ppm")),
        "printer": {**_machine(printer, PRINTER_STATUS, "print_time_left_s"),
                    "bedClear": printer.get("bed_clear", True)},
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

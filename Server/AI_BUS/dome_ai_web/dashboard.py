"""
Вычисление снимка приборной панели (карточки справа на веб-странице) на
основе РЕАЛЬНЫХ данных от DomeTelemetryClient — через уже существующий
ToolExecutor (тот же диспетчер, что используют агенты).

Если конкретный метод ещё не реализован на стороне client.py/сервера,
ToolExecutor.execute() вернёт {"error": ...} — соответствующая карточка в
этом случае получает нейтральную заглушку (0 / "—" / уровень "muted"),
не роняя остальные карточки.
"""

from typing import Any, Dict, Optional

from tool_registry import ToolExecutor

CardData = Dict[str, Any]


def _call(tool_executor: ToolExecutor, name: str) -> Optional[Dict[str, Any]]:
    """Вызывает инструмент и возвращает None, если он недоступен/вернул ошибку."""
    result = tool_executor.execute(name, {})
    if isinstance(result, dict) and "error" in result:
        return None
    return result


def _battery_card(tool_executor: ToolExecutor) -> CardData:
    result = _call(tool_executor, "get_battery")
    if result is None:
        return {"value": "0%", "n": 0, "sub": "нет данных", "level": "muted"}
    n = result.get("battery", 0) or 0
    level = "ok" if n >= 50 else "warn" if n >= 20 else "danger"
    return {"value": f"{n}%", "n": n, "sub": "заряд", "level": level}


def _power_card(tool_executor: ToolExecutor) -> CardData:
    result = _call(tool_executor, "get_solar_generation")
    if result is None:
        return {"value": "0 кВт", "sub": "нет данных", "level": "muted"}
    kw = result.get("solar_generation", 0) or 0
    level = "ok" if kw > 0.2 else "warn"
    return {"value": f"{kw:g} кВт", "sub": "генерация", "level": level}


def _load_card(tool_executor: ToolExecutor) -> CardData:
    result = _call(tool_executor, "get_power_summary")
    if result is None:
        return {"value": "0 кВт", "sub": "нет данных", "level": "muted"}
    # Схема get_power() в вашем client.py может отличаться — пробуем
    # несколько правдоподобных названий ключа, иначе честно показываем 0.
    load = None
    for key in ("total_load", "load", "consumption", "total_consumption"):
        if key in result:
            load = result[key]
            break
    if load is None:
        return {"value": "0 кВт", "sub": "ключ не найден в get_power_summary", "level": "muted"}
    load_kw = float(load) / 1000.0
    return {"value": f"{load_kw:.2f} кВт", "sub": "потребление", "level": "ok"}


def _air_card(tool_executor: ToolExecutor) -> CardData:
    result = _call(tool_executor, "get_co2")
    if result is None:
        return {"value": "0 ppm", "sub": "нет данных", "level": "muted"}
    ppm = result.get("co2", 0) or 0
    level = "ok" if ppm < 1000 else "warn" if ppm < 2000 else "danger"
    return {"value": f"{ppm} ppm", "sub": "CO2", "level": level}


def _temp_card(tool_executor: ToolExecutor) -> CardData:
    result = _call(tool_executor, "get_temperature")
    if result is None:
        return {"value": "0 °C", "sub": "нет данных", "level": "muted"}
    t = result.get("temperature", 0) or 0
    level = "ok" if 15 <= t <= 28 else "warn" if 5 <= t <= 35 else "danger"

    sub = "внутри"
    hum_result = _call(tool_executor, "get_humidity")
    if hum_result is not None:
        sub = f"внутри · влажность {hum_result.get('humidity', 0)}%"

    return {"value": f"{t:+g} °C", "sub": sub, "level": level}


def _link_card(tool_executor: ToolExecutor) -> CardData:
    result = _call(tool_executor, "get_signal_strength")
    if result is None:
        return {"value": "OFFLINE", "sub": "нет данных", "level": "muted"}
    signal = result.get("signal_strength", 0) or 0
    level = "ok" if signal >= 50 else "warn" if signal >= 10 else "danger"
    value = "ONLINE" if signal > 0 else "OFFLINE"
    return {"value": value, "sub": f"сигнал {signal}", "level": level}


def _production_card(tool_executor: ToolExecutor) -> CardData:
    printer = _call(tool_executor, "get_printer_status")
    mill = _call(tool_executor, "get_mill_status")
    if printer is None and mill is None:
        return {"value": "—", "sub": "нет данных", "level": "muted"}

    p_state = str((printer or {}).get("state", "н/д"))
    m_state = str((mill or {}).get("state", "н/д"))
    level = "danger" if "ошиб" in p_state.lower() or "ошиб" in m_state.lower() else "ok"
    return {"value": f"3D: {p_state}", "sub": f"CNC: {m_state}", "level": level}


def _mode_card(cards: Dict[str, CardData]) -> CardData:
    """Агрегированный статус системы — считается локально из уровней остальных карточек."""
    levels = [c["level"] for c in cards.values()]
    if "danger" in levels:
        return {"value": "Авария", "level": "danger"}
    if "warn" in levels:
        return {"value": "Внимание", "level": "warn"}
    if all(l == "muted" for l in levels):
        return {"value": "Нет данных", "level": "muted"}
    return {"value": "Штатный", "level": "ok"}


def compute_snapshot(tool_executor: ToolExecutor) -> Dict[str, CardData]:
    """Возвращает словарь карточек в формате, ожидаемом фронтендом (app.js)."""
    cards: Dict[str, CardData] = {
        "battery": _battery_card(tool_executor),
        "power": _power_card(tool_executor),
        "load": _load_card(tool_executor),
        "air": _air_card(tool_executor),
        "temp": _temp_card(tool_executor),
        "link": _link_card(tool_executor),
        "production": _production_card(tool_executor),
    }
    cards["mode"] = _mode_card(cards)
    return cards
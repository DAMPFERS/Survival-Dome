"""
Определение инструментов (tools) для function calling.
Эти инструменты позволяют AI управлять системами купола.

Хранитель подключается к серверу купола с ключом администратора, поэтому
сервер его не ограничивает. Граница того, что Хранитель может сделать по
просьбе игроков, — KEEPER_ALLOWED_ACTIONS: любые вызовы проверяются по нему
(в том числе после попыток игроков "уговорить" модель).
"""
from __future__ import annotations

import json
from typing import Any

# Узел -> действия, которые Хранитель вправе выполнять. Имена — основные имена
# действий сервера (см. describe_nodes). Сценарные/админские действия
# (эмуляция аварий, режимы искажений, ручные "починки") сюда намеренно не входят.
KEEPER_ALLOWED_ACTIONS: dict[str, list[str]] = {
    "solar_panels_01": ["enable_all_mppt", "disable_all_mppt", "enable_mppt", "disable_mppt"],
    "solar_inverter_01": ["set_mode_hybrid", "set_mode_grid_only", "reset_error", "start_cell_balancing"],
    "wind_turbine_01": ["turn_on", "turn_off"],
    "dizel_1": ["turn_on", "turn_off"],
    "smart_panel_01": ["line_on", "line_off", "reset_protection", "reset_line_error"],
    "printer_3d_01": ["pause", "resume", "abort", "home", "start_job"],
    "cnc_01": ["feed_hold", "resume", "abort", "home", "start_job", "probe_z", "set_zero"],
    "fume_extraction_01": ["turn_on", "turn_off", "reset_alarm"],
    "supply_ventilation_01": ["turn_on", "turn_off", "set_speed", "set_mode", "reset_alarm"],
    "equipment_cooling_01": ["turn_on", "turn_off", "set_speed"],
    # МС-ТЮК: теплица ФабЛаб и теплица Химлаб (реальные стенды)
    "thermal_insulation_01": ["set_fan_pwm", "set_vacuum", "set_lock"],
    "thermal_insulation_02": ["set_fan_pwm", "set_relay_fan_level", "set_lock"],
    "water_pump_01": ["turn_on", "turn_off", "reset_alarm"],
    "water_filter_01": ["switch_to_reserve", "force_flush"],
    "dome_sealing_01": ["open_valves", "close_valves", "seal_zone"],
    "smoke_detector_01": ["reset_alarm"],
    "radio_01": ["set_frequency", "set_protocol"],
    "backup_comms_01": ["switch_channel"],
}

# Узлы, состояние которых Хранитель может запросить
STATUS_NODES: list[str] = [
    "solar_panels_01", "solar_inverter_01", "battery_01", "wind_turbine_01", "dizel_1", "fuel_tank_01",
    "smart_panel_01", "printer_3d_01", "cnc_01", "material_inventory_01", "equipment_cooling_01",
    "climate_sensor_01", "climate_sensor_02", "climate_sensor_03", "air_quality_sensor_01",
    "smoke_detector_01", "fume_extraction_01", "supply_ventilation_01", "dome_sealing_01",
    "thermal_insulation_01", "thermal_insulation_02", "weather_station_01", "seysmo_01", "radiation_sensor_01",
    "chem_sensor_01", "smoke_detector_02", "water_pump_01", "water_tank_01", "water_filter_01",
    "fire_suppression_01", "access_control_01", "radio_01", "backup_comms_01", "edge_compute_01",
    "network_link_01", "dome_automation_01",
]


def keeper_can(node_id: str, action: str) -> bool:
    return action in KEEPER_ALLOWED_ACTIONS.get(node_id, [])


def _allowed_summary() -> str:
    return "; ".join(f"{nid}: {', '.join(actions)}" for nid, actions in KEEPER_ALLOWED_ACTIONS.items())


# Схема инструментов в формате OpenAI/DeepSeek/Mistral
DOME_CONTROL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "control_power_line",
            "description": "Включить или выключить линию умного электрического щитка купола (линии 1–8: "
                           "1 производственный отдел — рабочее место и вытяжка FabLab, 2 теплица ФабЛаб, "
                           "3 3D-принтер, 4 ЧПУ-фрезер, 5 теплица Химлаб, 6 основное освещение, "
                           "7 аварийное освещение, 8 резервная). Линию со сработавшей защитой "
                           "возвращает действие reset_protection (control_dome_node).",
            "parameters": {
                "type": "object",
                "properties": {
                    "line_number": {"type": "integer", "minimum": 1, "maximum": 8, "description": "Номер линии (1-8)"},
                    "action": {"type": "string", "enum": ["on", "off"], "description": "on — включить, off — выключить"},
                },
                "required": ["line_number", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_diesel_generator",
            "description": "Запуск или остановка резервного дизельного генератора купола.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["start", "stop"], "description": "start — запустить, stop — остановить"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_production",
            "description": "Управление производственным оборудованием: 3D-принтером или ЧПУ-фрезером "
                           "(пауза, продолжение, прерывание задания, поиск нулевого положения, запуск задания "
                           "по файлу из памяти станка). Только для ЧПУ (станок в IDLE, без задания): "
                           "probe_z — поиск поверхности заготовки пробником и установка Z0 (пробник подключён); "
                           "set_zero — текущая точка становится нулём рабочих координат по осям axes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "machine": {"type": "string", "enum": ["printer", "cnc"], "description": "printer — 3D-принтер, cnc — ЧПУ-фрезер"},
                    "action": {"type": "string",
                               "enum": ["pause", "resume", "abort", "home", "start_job", "probe_z", "set_zero"],
                               "description": "start_job — запустить печать/фрезеровку файла; "
                                              "probe_z и set_zero — только cnc"},
                    "file_name": {"type": "string",
                                  "description": "Для start_job: точное имя файла из списка файлов станка "
                                                 "(раздел «ТЕКУЩЕЕ СОСТОЯНИЕ КУПОЛА»)"},
                    "plate_thickness": {"type": "number",
                                        "description": "Для probe_z: толщина пластины пробника, мм (0–30, по умолчанию 0)"},
                    "distance": {"type": "number",
                                 "description": "Для probe_z: максимальный ход вниз, мм (0.1–30, по умолчанию 10)"},
                    "axes": {"type": "string", "enum": ["xy", "x", "y", "z", "xyz"],
                             "description": "Для set_zero: какие оси обнулить в текущей точке"},
                },
                "required": ["machine", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_ventilation",
            "description": "Управление вентиляцией: приточной вентиляцией с рекуператором (supply) "
                           "или вытяжкой FabLab (fume).",
            "parameters": {
                "type": "object",
                "properties": {
                    "system": {"type": "string", "enum": ["supply", "fume"]},
                    "action": {"type": "string", "enum": ["on", "off", "set_speed", "set_mode"],
                               "description": "set_speed и set_mode — только для supply"},
                    "value": {"type": "string",
                              "description": "Для set_speed — скорость 10–100 (%); для set_mode — RECIRCULATION, FRESH_AIR или MIXED"},
                },
                "required": ["system", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_greenhouse",
            "description": "Управление теплицами МС-ТЮК. fablab — теплица ФабЛаб: 6 вентиляторов (0–3 вдув, "
                           "4–5 выдув), пылесос, 2 замка (0, 1). chemlab — теплица Химлаб: 2 вентилятора "
                           "(0 вдув, 1 выдув), релейный вентилятор (уровень 0–10), общий замок на обе двери.",
            "parameters": {
                "type": "object",
                "properties": {
                    "greenhouse": {"type": "string", "enum": ["fablab", "chemlab"]},
                    "action": {"type": "string", "enum": ["fan", "relay_fan", "vacuum", "lock", "unlock"],
                               "description": "fan — ШИМ вентилятора; relay_fan — только chemlab; "
                                              "vacuum — только fablab; lock/unlock — замок"},
                    "fan": {"type": "integer", "description": "Для fan: индекс вентилятора"},
                    "pwm": {"type": "integer", "description": "Для fan: ШИМ 0–255 (ниже ~30 вентилятор стоит)"},
                    "level": {"type": "integer", "description": "Для relay_fan: уровень 0–10"},
                    "on": {"type": "boolean", "description": "Для vacuum: включить (true) или выключить (false)"},
                    "lock_index": {"type": "integer", "description": "Для lock/unlock в fablab: номер замка 0 или 1"},
                },
                "required": ["greenhouse", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_dome_node",
            "description": "Выполнить другое управляющее действие над узлом купола. Разрешённые действия: "
                           + _allowed_summary(),
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "enum": list(KEEPER_ALLOWED_ACTIONS)},
                    "action": {"type": "string", "description": "Имя действия из списка разрешённых для узла"},
                    "value": {"type": "string",
                              "description": "Параметр действия, если нужен: число, строка или JSON-объект строкой"},
                },
                "required": ["node_id", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_status",
            "description": "Получить подробное текущее состояние узла купола.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "enum": STATUS_NODES},
                },
                "required": ["node_id"],
            },
        },
    },
]


def _parse_value(value: Any) -> Any:
    """LLM передаёт value строкой: числа и JSON-объекты разбираем, остальное оставляем строкой."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _resolve_greenhouse(arguments: dict[str, Any]) -> tuple[str, str, Any]:
    greenhouse, action = arguments.get("greenhouse"), arguments.get("action")
    node_id = {"fablab": "thermal_insulation_01", "chemlab": "thermal_insulation_02"}.get(greenhouse)
    if not node_id:
        raise ValueError("Теплица должна быть fablab или chemlab")
    if action == "fan":
        if arguments.get("fan") is None or arguments.get("pwm") is None:
            raise ValueError("Для вентилятора нужны fan и pwm")
        return node_id, "set_fan_pwm", {"fan": int(arguments["fan"]), "pwm": int(arguments["pwm"])}
    if action == "relay_fan" and greenhouse == "chemlab":
        if arguments.get("level") is None:
            raise ValueError("Для релейного вентилятора нужен level (0–10)")
        return node_id, "set_relay_fan_level", int(arguments["level"])
    if action == "vacuum" and greenhouse == "fablab":
        return node_id, "set_vacuum", 1 if arguments.get("on", True) else 0
    if action in ("lock", "unlock"):
        locked = action == "lock"
        if greenhouse == "chemlab":
            return node_id, "set_lock", locked
        return node_id, "set_lock", {"lock": int(arguments.get("lock_index", 0)), "locked": locked}
    raise ValueError(f"Действие {action} недоступно для теплицы {greenhouse}")


def resolve_tool_call(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str, Any]:
    """
    Переводит вызов инструмента управления в (node_id, action, value).
    Бросает ValueError, если аргументы некорректны или действие не разрешено Хранителю.
    """
    if tool_name == "control_power_line":
        line = int(arguments.get("line_number", 0))
        if not 1 <= line <= 8:
            raise ValueError("Номер линии должен быть от 1 до 8")
        action = {"on": "line_on", "off": "line_off"}.get(arguments.get("action"))
        node_id, value = "smart_panel_01", line
    elif tool_name == "control_diesel_generator":
        action = {"start": "turn_on", "stop": "turn_off"}.get(arguments.get("action"))
        node_id, value = "dizel_1", None
    elif tool_name == "control_production":
        machine = arguments.get("machine")
        node_id = {"printer": "printer_3d_01", "cnc": "cnc_01"}.get(machine)
        action = arguments.get("action")
        value = None
        if machine == "cnc" and action == "pause":
            action = "feed_hold"
        elif action == "start_job":
            file_name = str(arguments.get("file_name") or "").strip()
            if not file_name:
                raise ValueError("Для запуска задания нужно имя файла (file_name)")
            value = {"file_name": file_name}
        elif action in ("probe_z", "set_zero") and machine != "cnc":
            raise ValueError("Пробинг Z и установка нуля доступны только для ЧПУ-фрезера")
        elif action == "set_zero":
            axes = str(arguments.get("axes") or "").strip().lower()
            if not axes:
                raise ValueError("Для установки нуля нужно указать оси (axes), например xy")
            value = {"axes": axes}
        elif action == "probe_z":
            # остальные параметры (feed, retract, ...) — значения сервера по умолчанию
            value = {key: float(arguments[key]) for key in ("plate_thickness", "distance")
                     if arguments.get(key) is not None} or None
    elif tool_name == "control_ventilation":
        node_id = {"supply": "supply_ventilation_01", "fume": "fume_extraction_01"}.get(arguments.get("system"))
        action = {"on": "turn_on", "off": "turn_off"}.get(arguments.get("action"), arguments.get("action"))
        value = arguments.get("value")
    elif tool_name == "control_greenhouse":
        node_id, action, value = _resolve_greenhouse(arguments)
    elif tool_name == "control_dome_node":
        node_id, action, value = arguments.get("node_id"), arguments.get("action"), arguments.get("value")
    else:
        raise ValueError(f"Неизвестный инструмент: {tool_name}")

    if not node_id or not action:
        raise ValueError("Некорректные аргументы команды")
    value = _parse_value(value)
    if not keeper_can(node_id, action):
        raise ValueError(f"Хранителю не разрешено действие {action} для {node_id}")
    return node_id, action, value

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
    "printer_3d_01": ["pause", "resume", "abort", "home"],
    "cnc_01": ["feed_hold", "resume", "abort", "home"],
    "fume_extraction_01": ["turn_on", "turn_off", "reset_alarm"],
    "supply_ventilation_01": ["turn_on", "turn_off", "set_speed", "set_mode", "reset_alarm"],
    "equipment_cooling_01": ["turn_on", "turn_off", "set_speed"],
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
    "thermal_insulation_01", "weather_station_01", "seysmo_01", "radiation_sensor_01", "chem_sensor_01",
    "water_pump_01", "water_tank_01", "water_filter_01", "fire_suppression_01", "access_control_01",
    "radio_01", "backup_comms_01", "edge_compute_01",
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
                           "1 производственный отдел, 2 ЧПУ-фрезер, 3 3D-принтер, 4 освещение, "
                           "5 аварийное освещение, 6 вытяжка FabLab, 7–8 резервные).",
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
                           "(пауза, продолжение, прерывание задания, поиск нулевого положения).",
            "parameters": {
                "type": "object",
                "properties": {
                    "machine": {"type": "string", "enum": ["printer", "cnc"], "description": "printer — 3D-принтер, cnc — ЧПУ-фрезер"},
                    "action": {"type": "string", "enum": ["pause", "resume", "abort", "home"]},
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
        if machine == "cnc" and action == "pause":
            action = "feed_hold"
        value = None
    elif tool_name == "control_ventilation":
        node_id = {"supply": "supply_ventilation_01", "fume": "fume_extraction_01"}.get(arguments.get("system"))
        action = {"on": "turn_on", "off": "turn_off"}.get(arguments.get("action"), arguments.get("action"))
        value = arguments.get("value")
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

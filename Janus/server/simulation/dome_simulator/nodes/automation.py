# dome_simulator/nodes/automation.py
"""
Автоматика купола (ПЛК) — узел dome_automation_01 (dome_crises.md, п. 3.3, Д3).

Интерлоки срабатывают по ТЕЛЕМЕТРИИ (с подменами показаний), а не по истинным
значениям — поэтому ложные данные опасны ровно тогда, когда им доверяет
автоматика. Узел хранит состояние интерлоков, доверие к источникам и журнал;
сами условия и действия вычисляет правило automation_rule (rules.py), которому
доступны телеметрия и команды другим узлам.

Интерлоки:
    IL_CO2          CO₂ > 2000 ppm (60 с)        → пауза принтера и ЧПУ, приток FRESH_AIR 100 %
    IL_CONDENSATE   RH > 90 % или T − Tросы < 1 °C (60 с) → пауза ЧПУ
    IL_SMOKE        датчик дыма зоны в ALARM     → FABLAB: пауза станков, линия 1 выкл.;
                                                   через 60 с без квитирования — пожаротушение зоны
    IL_LOAD_SHED    SOC < 25 % (120 с), прогноз автономности < 1 ч без внешнего резерва (180 с)
                    или дефицит мощности (20 с)  → линии 8, 7, 1; при продолжении — 6, 3, 2;
                                                   автозапуск дизеля
    IL_STORM        ветер > 20 м/с (30 с)        → ветрогенератор выкл., клапаны закрыты, RECIRCULATION
    IL_OVERVOLT     напряжение линии > 245 В (120 с) → отключение линии
    IL_INV_OVERLOAD инвертор сообщает E07 (120 с) → линии 8, 7, 1
    IL_HEAT         температура инвертора > 75 °C (120 с) → линии 1, 6
    IL_OVERCURRENT  ток линии > 90 % уставки (120 с) → отключение линии (уставка линии 1 —
                    бюджет рабочего места 2 А, остальных — номинал автомата)

Команды агента: set_data_trust, block_interlock (≤ 30 мин), unblock_interlock,
reset_interlock («снять» сработавший интерлок: он перевзводится, отключённые им
линии включаются обратно, вентиляция возвращается в прежний режим), acknowledge,
set_primary_sensor (основной датчик CO₂ для автоматики), set_ventilation_auto.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..events import Severity
from .base import register_node_type
from .sandbox import ControlError, SandboxNode, control, parse_bool, parse_choice, parse_number

INTERLOCKS: dict[str, dict[str, Any]] = {
    "IL_CO2": {"title": "CO₂ > 2000 ppm", "delay_s": 60.0},
    "IL_CONDENSATE": {"title": "Конденсат: RH > 90 % или T − Tросы < 1 °C", "delay_s": 60.0},
    "IL_SMOKE": {"title": "Дым в зоне", "delay_s": 0.0, "suppression_delay_s": 60.0},
    "IL_LOAD_SHED": {"title": "Сброс нагрузки: SOC < 25 % / автономность < 1 ч / дефицит",
                     "soc_delay_s": 120.0, "autonomy_delay_s": 180.0, "deficit_delay_s": 20.0,
                     "stage2_delay_s": 300.0},
    "IL_STORM": {"title": "Штормовой режим: ветер > 20 м/с", "delay_s": 30.0},
    "IL_OVERVOLT": {"title": "Перенапряжение линии > 245 В", "delay_s": 120.0},
    "IL_INV_OVERLOAD": {"title": "Перегрузка инвертора (E07)", "delay_s": 120.0},
    "IL_HEAT": {"title": "Перегрев инвертора > 75 °C", "delay_s": 120.0},
    "IL_OVERCURRENT": {"title": "Ток линии > 90 % уставки", "delay_s": 120.0},
}
TRUST_LEVELS = ("TRUSTED", "UNRELIABLE", "FAILED")
CO2_SENSORS = ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03")
MAX_BLOCK_MIN = 30.0
EVENTS_KEPT = 60

_LINES_RE = re.compile(r"^lines\[(\d+)\]\.(\w+)$")


def trust_key(node_id: str, param: str) -> str:
    return f"{node_id}.{param}"


def normalize_param(value: dict[str, Any]) -> str:
    """param может быть 'co2_ppm', 'lines[1].voltage_v' (индекс JSON с нуля),
    или param='voltage_v' + line=2 (номер линии с единицы)."""
    param = str(value.get("param", "")).strip()
    if not param:
        raise ControlError('set_data_trust: не указан param')
    if value.get("line") is not None and "[" not in param:
        number = int(parse_number(value["line"], 1, 8, "Номер линии"))
        return f"lines[{number - 1}].{param}"
    if param != "*" and not re.match(r"^[\w\[\]\.]+$", param):
        raise ControlError(f"set_data_trust: некорректный param {param!r}")
    return param


@register_node_type("dome_automation")
class DomeAutomation(SandboxNode):
    title = "Автоматика купола (ПЛК)"
    system = "Автоматика"
    category = "automation"

    def __init__(self, node_id: str, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self._event_seq = 0

    def initial_state(self) -> dict[str, Any]:
        return {
            "mode": "AUTO",
            "interlocks": {il: {"state": "ARMED", "pending_s": 0.0, "blocked_remaining_s": 0.0,
                                "trips": 0, "reason": None, "title": spec["title"]}
                           for il, spec in INTERLOCKS.items()},
            "data_trust": {},
            "events": [],
            "primary_co2_sensor": "climate_sensor_01",
            "ventilation_auto": True,
            "diesel_autostart": True,
            "line_current_limits_a": {"1": 2.0},
            "autonomy_forecast_h": None,
            "control_requests": [],
        }

    # ---------------- журнал ----------------

    def next_event_id(self) -> str:
        self._event_seq += 1
        return f"AUT-{self._event_seq:04d}"

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        interlocks = {k: dict(v) for k, v in node["interlocks"].items()}
        changed = False
        for il, st in interlocks.items():
            if st["state"] == "BLOCKED":
                st["blocked_remaining_s"] = max(0.0, st["blocked_remaining_s"] - gdt)
                changed = True
                if st["blocked_remaining_s"] <= 0:
                    st.update(state="ARMED", pending_s=0.0)
                    self.emit("automation.interlock_unblocked", Severity.INFO, interlock=il)
        return {"interlocks": interlocks} if changed else {}

    # ---------------- управление ----------------

    @staticmethod
    def _interlock_id(value: Any) -> str:
        il = value.get("id") if isinstance(value, dict) else value
        if not isinstance(il, str) or il.strip().upper() not in INTERLOCKS:
            raise ControlError(f"Интерлок: ожидалось одно из {list(INTERLOCKS)}, получено {il!r}")
        return il.strip().upper()

    @control("set_data_trust")
    def _set_data_trust(self, node, value):
        """{"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"}.
        Интерлоки не используют источники с доверием UNRELIABLE и FAILED."""
        if not isinstance(value, dict) or not value.get("node"):
            raise ControlError('set_data_trust: ожидалось {"node", "param", "trust"}')
        node_id = str(value["node"]).strip()
        param = normalize_param(value)
        trust = parse_choice(value.get("trust", "UNRELIABLE"), TRUST_LEVELS, "Доверие")
        data_trust = dict(node["data_trust"])
        key = trust_key(node_id, param)
        if trust == "TRUSTED":
            data_trust.pop(key, None)
        else:
            data_trust[key] = trust
        self.emit("automation.data_trust", Severity.INFO, source=key, trust=trust)
        return {"data_trust": data_trust}

    @control("block_interlock")
    def _block(self, node, value):
        """{"id": "IL_SMOKE", "minutes": 10} — не более 30 минут."""
        il = self._interlock_id(value)
        minutes = parse_number(value.get("minutes", 10) if isinstance(value, dict) else 10,
                               0.1, MAX_BLOCK_MIN, "Длительность блокировки, мин")
        interlocks = {k: dict(v) for k, v in node["interlocks"].items()}
        interlocks[il].update(state="BLOCKED", blocked_remaining_s=minutes * 60.0, pending_s=0.0)
        self.emit("automation.interlock_blocked", Severity.WARNING, interlock=il, minutes=minutes)
        return {"interlocks": interlocks}

    @control("unblock_interlock")
    def _unblock(self, node, value):
        il = self._interlock_id(value)
        interlocks = {k: dict(v) for k, v in node["interlocks"].items()}
        if interlocks[il]["state"] == "BLOCKED":
            interlocks[il].update(state="ARMED", blocked_remaining_s=0.0, pending_s=0.0)
        return {"interlocks": interlocks}

    @control("reset_interlock", "release_interlock")
    def _reset(self, node, value):
        """Снять сработавший интерлок: перевзвести и отменить его отключения."""
        il = self._interlock_id(value)
        requests = list(node["control_requests"]) + [{"op": "release", "id": il}]
        return {"control_requests": requests}

    @control("acknowledge", "ack")
    def _acknowledge(self, node, value):
        """{"event_id": "AUT-0003"} или "all". Квитирование IL_SMOKE отменяет автопуск пожаротушения."""
        event_id = value.get("event_id") if isinstance(value, dict) else value
        events = [dict(e) for e in node["events"]]
        found = False
        for e in events:
            if event_id in ("all", "ALL") or e["event_id"] == event_id:
                e["acknowledged"] = True
                found = True
        if not found:
            raise ControlError(f"Событие {event_id!r} не найдено в журнале автоматики")
        return {"events": events}

    @control("set_primary_sensor")
    def _set_primary(self, node, value):
        """Основной датчик CO₂ для автоматики (вентиляция по CO₂, IL_CO2)."""
        sensor = value.get("node") if isinstance(value, dict) else value
        if sensor not in CO2_SENSORS:
            raise ControlError(f"Датчик: ожидалось одно из {list(CO2_SENSORS)}, получено {sensor!r}")
        return {"primary_co2_sensor": sensor}

    @control("set_ventilation_auto")
    def _vent_auto(self, node, value):
        """Автоматическое управление притоком по CO₂ (вкл/выкл)."""
        return {"ventilation_auto": parse_bool(value)}

    @control("set_diesel_autostart")
    def _diesel_auto(self, node, value):
        return {"diesel_autostart": parse_bool(value)}

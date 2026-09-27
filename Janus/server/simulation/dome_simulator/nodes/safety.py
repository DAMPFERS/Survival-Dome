# dome_simulator/nodes/safety.py
"""
Пожарная безопасность и контроль доступа (разделы 4.3, 5.4, 6 реестра
dome_sandbox_nodes.md): smoke_detector, fire_suppression, access_control.
"""
from __future__ import annotations

from typing import Any, Optional

from ..environment import relax
from ..events import Severity
from .base import register_node_type
from .sandbox import ControlError, SandboxNode, SensorNode, control, parse_bool, parse_choice

# ---------------------------------------------------------------------------
# 4.3 Датчик дыма
# ---------------------------------------------------------------------------


@register_node_type("smoke_detector")
class SmokeDetector(SensorNode):
    """
    Оптический датчик. Тревога фиксируется (latch) и держится до RESET_ALARM.
    Редкие ложные срабатывания — пыль/пар (false_alarm_rate_per_hour).
    Дым берётся из env.indoor.smoke_density (его будут поднимать кризисы).
    """
    title = "Датчик дыма / пожарный датчик"
    system = "Пожарная безопасность"
    category = "safety"

    THRESHOLD = 0.08  # оптическая плотность, при которой срабатывает датчик

    def __init__(self, node_id: str, zone: str = "FABLAB", false_alarm_rate_per_hour: float = 0.004,
                 glitch_rate_per_hour: float = 0.005, seed: Optional[int] = None) -> None:
        super().__init__(node_id, glitch_rate_per_hour, seed)
        self.zone = zone
        self.false_alarm_rate_per_hour = false_alarm_rate_per_hour
        self._false_smoke_s = 0.0

    def initial_state(self) -> dict[str, Any]:
        return {"zone": self.zone, "smoke_detected": False, "obscuration_pct": 0.0,
                "alarm_state": "NORMAL", "false_alarm_suspected": False}

    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> dict[str, Any]:
        density = self.env.indoor.smoke_density
        false_alarm = False
        if self._false_smoke_s > 0:
            self._false_smoke_s -= gdt
            density = max(density, 0.12)
            false_alarm = True
        elif self.happens(self.false_alarm_rate_per_hour, gdt):
            self._false_smoke_s = self.rng.uniform(60.0, 400.0)
            density = max(density, 0.12)
            false_alarm = True

        measured = max(0.0, density + self.noise(0.003 * noise_k))
        detected = measured >= self.THRESHOLD
        alarm = node["alarm_state"]
        if detected and alarm != "ALARM":
            alarm = "ALARM"
            self.emit("smoke.alarm", Severity.CRITICAL, zone=self.zone, obscuration_pct=round(measured * 100, 1))
        return {
            "smoke_detected": detected,
            "obscuration_pct": round(measured * 100, 2),
            "alarm_state": alarm,
            "false_alarm_suspected": false_alarm and self.env.indoor.smoke_density < self.THRESHOLD
                                     and node["confidence"] < 0.9,
        }

    @control("reset_alarm")
    def _reset_alarm(self, node, value):
        # Если дым всё ещё есть — тревога вернётся на следующем тике
        return {"alarm_state": "NORMAL"}


# ---------------------------------------------------------------------------
# 5.4 Система пожаротушения
# ---------------------------------------------------------------------------

FIRE_ZONES = ("FABLAB", "LIVING", "STORAGE", "POWER_ROOM")


@register_node_type("fire_suppression")
class FireSuppression(SandboxNode):
    """
    Спринклерная система. В дежурном режиме жокей-насос держит давление
    ~6 бар, компенсируя медленную утечку. При пуске зоны давление падает,
    клапан зоны открыт. Автопуск от датчиков дыма появится с правилами —
    вход control_auto_trigger_zone (зона, которую требует потушить автоматика).
    """
    title = "Система пожаротушения / спринклеры"
    system = "Пожарная безопасность"
    category = "safety"

    STANDBY_BAR = 6.0

    def __init__(self, node_id: str, false_trigger_rate_per_hour: float = 0.003,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.false_trigger_rate_per_hour = false_trigger_rate_per_hour

    def initial_state(self) -> dict[str, Any]:
        return {
            "pressure_bar": self.STANDBY_BAR,
            "valves": {zone: "CLOSED" for zone in FIRE_ZONES},
            "ready": True,
            "false_trigger_count": 0,
            "state": "READY",              # READY | ACTIVE | FAULT | DISABLED
            "active_zone": None,
            "automation_blocked": False,
            "alarm": False,
            "fault_code": None,
            "control_auto_trigger_zone": None,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        state, zone = node["state"], node["active_zone"]
        u: dict[str, Any] = {}

        auto_zone = node["control_auto_trigger_zone"]
        if auto_zone and state == "READY" and not node["automation_blocked"]:
            state, zone = "ACTIVE", auto_zone
            u.update(alarm=True, valves={z: ("OPEN" if z == zone else "CLOSED") for z in FIRE_ZONES})
            self.emit("fire_suppression.activated", Severity.CRITICAL, zone=zone, source="auto")

        # Ложный сигнал датчика потока/давления — фиксируется, но пуска нет
        if state in ("READY", "DISABLED") and self.happens(self.false_trigger_rate_per_hour, gdt):
            u["false_trigger_count"] = node["false_trigger_count"] + 1
            self.emit("fire_suppression.false_trigger", Severity.WARNING)

        if state == "ACTIVE":
            pressure = relax(node["pressure_bar"], 2.5, 300, gdt)
        else:
            pressure = relax(node["pressure_bar"], self.STANDBY_BAR, 600, gdt)
        pressure += self.noise(0.02)

        fault_code = node["fault_code"]
        if state != "ACTIVE" and pressure < 4.0 and fault_code is None:
            fault_code = "LOW_PRESSURE"
            state = "FAULT"
            self.emit("fire_suppression.fault", Severity.WARNING, fault_code=fault_code)
        elif state == "FAULT" and fault_code == "LOW_PRESSURE" and pressure > 5.5:
            fault_code = None
            state = "DISABLED" if node["automation_blocked"] else "READY"

        u.update(
            pressure_bar=round(pressure, 2),
            state=state,
            active_zone=zone,
            fault_code=fault_code,
            ready=state == "READY",
        )
        return u

    @control("manual_start_zone", "start_zone")
    def _start_zone(self, node, value):
        zone = parse_choice(value, FIRE_ZONES, "Зона пожаротушения")
        if node["state"] == "FAULT":
            raise ControlError(f"Система в аварии ({node['fault_code']})")
        self.emit("fire_suppression.activated", Severity.CRITICAL, zone=zone, source="manual")
        return {"state": "ACTIVE", "active_zone": zone, "alarm": True, "ready": False,
                "valves": {z: ("OPEN" if z == zone else "CLOSED") for z in FIRE_ZONES}}

    @control("stop")
    def _stop(self, node, value):
        if node["state"] != "ACTIVE":
            raise ControlError("Пожаротушение не активно")
        return {"state": "DISABLED" if node["automation_blocked"] else "READY", "active_zone": None,
                "valves": {z: "CLOSED" for z in FIRE_ZONES}}

    @control("reset_alarm")
    def _reset_alarm(self, node, value):
        return {"alarm": False, "fault_code": None if node["state"] != "FAULT" else node["fault_code"]}

    @control("block_automation")
    def _block(self, node, value):
        blocked = parse_bool(value)
        u: dict[str, Any] = {"automation_blocked": blocked}
        if node["state"] in ("READY", "DISABLED"):
            u["state"] = "DISABLED" if blocked else "READY"
        return u

    @control("unblock_automation")
    def _unblock(self, node, value):
        return self._block(node, False)


# ---------------------------------------------------------------------------
# 6 (3.1) Система контроля доступа
# ---------------------------------------------------------------------------

DOORS = ("MAIN_AIRLOCK", "FABLAB", "STORAGE", "SERVER_ROOM")
DOOR_TRAFFIC_PER_HOUR = {"MAIN_AIRLOCK": 3.0, "FABLAB": 4.0, "STORAGE": 1.0, "SERVER_ROOM": 0.3}
PEOPLE = ("OP-01", "OP-02", "OP-03", "ENG-01", "ENG-02", "MED-01", "SCI-01", "SCI-02")


@register_node_type("access_control")
class AccessControl(SandboxNode):
    """
    Двери с электрозамками. Проходы генерируются по времени суток; изредка —
    попытка вскрытия (FORCED) или вмешательство в замок (TAMPER), что даёт тревогу.
    В режиме lockdown все двери заперты, проходы отклоняются.
    """
    title = "Система контроля доступа / двери"
    system = "Безопасность"
    category = "security"

    EVENTS_KEPT = 20

    def __init__(self, node_id: str, intrusion_rate_per_hour: float = 0.002,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.intrusion_rate_per_hour = intrusion_rate_per_hour

    def initial_state(self) -> dict[str, Any]:
        return {
            "doors": {d: {"state": "CLOSED", "locked": True, "lock_battery_pct": round(self.rng.uniform(70, 100), 1)}
                      for d in DOORS},
            "passage_events": [],
            "system_state": "NORMAL",      # NORMAL | ALARM | LOCKDOWN
            "alarm": False,
            "alarm_reason": None,
            "lockdown": False,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        doors = {d: dict(v) for d, v in node["doors"].items()}
        events = list(node["passage_events"])
        alarm, reason = node["alarm"], node["alarm_reason"]
        hour = self.hour
        activity = 0.1 if (hour < 7 or hour >= 22) else 1.0
        stamp = round(self.env.game_time_s / 3600.0, 2)

        for name, door in doors.items():
            # Дверь закрывается доводчиком; FORCED/TAMPER держатся до сброса тревоги
            if door["state"] == "OPEN":
                door["state"] = "CLOSED"
            door["lock_battery_pct"] = round(max(0.0, door["lock_battery_pct"] - 0.7 * gdt / 86400.0), 5)  # ~0.7 %/сутки

            if door["state"] == "CLOSED" and self.happens(DOOR_TRAFFIC_PER_HOUR[name] * activity, gdt):
                person = self.rng.choice(PEOPLE)
                granted = not node["lockdown"] and (name != "SERVER_ROOM" or person.startswith(("ENG", "OP")))
                events.append({"t_h": stamp, "door": name, "credential": person,
                               "direction": self.rng.choice(("IN", "OUT")),
                               "result": "GRANTED" if granted else "DENIED"})
                if granted:
                    door["state"] = "OPEN"

            if door["state"] == "CLOSED" and self.happens(self.intrusion_rate_per_hour, gdt):
                door["state"] = self.rng.choice(("FORCED", "TAMPER"))
                alarm, reason = True, f"{name}_{door['state']}"
                self.emit("access.alarm", Severity.CRITICAL, door=name, state=door["state"])

            if door["lock_battery_pct"] < 10.0 and not alarm:
                alarm, reason = True, f"{name}_LOCK_BATTERY_LOW"
                self.emit("access.alarm", Severity.WARNING, door=name, state="LOCK_BATTERY_LOW")

        if node["lockdown"]:
            system_state = "LOCKDOWN"
        else:
            system_state = "ALARM" if alarm else "NORMAL"
        return {"doors": doors, "passage_events": events[-self.EVENTS_KEPT:],
                "alarm": alarm, "alarm_reason": reason, "system_state": system_state}

    def _door(self, value: Any) -> str:
        return parse_choice(value, DOORS, "Дверь")

    def _set_door(self, node: dict[str, Any], name: str, **changes: Any) -> dict[str, Any]:
        doors = {d: dict(v) for d, v in node["doors"].items()}
        doors[name].update(changes)
        return {"doors": doors}

    @control("unlock_door")
    def _unlock(self, node, value):
        if node["lockdown"]:
            raise ControlError("Активна принудительная блокировка (lockdown)")
        return self._set_door(node, self._door(value), locked=False)

    @control("lock_door")
    def _lock(self, node, value):
        return self._set_door(node, self._door(value), locked=True)

    @control("reset_alarm")
    def _reset_alarm(self, node, value):
        doors = {d: {**v, "state": "CLOSED" if v["state"] in ("FORCED", "TAMPER") else v["state"]}
                 for d, v in node["doors"].items()}
        return {"doors": doors, "alarm": False, "alarm_reason": None,
                "system_state": "LOCKDOWN" if node["lockdown"] else "NORMAL"}

    @control("lockdown", "force_lock")
    def _lockdown(self, node, value):
        enabled = parse_bool(value)
        doors = node["doors"]
        if enabled:
            doors = {d: {**v, "locked": True} for d, v in doors.items()}
        return {"doors": doors, "lockdown": enabled,
                "system_state": "LOCKDOWN" if enabled else ("ALARM" if node["alarm"] else "NORMAL")}

    @control("replace_lock_battery")
    def _replace_battery(self, node, value):
        return self._set_door(node, self._door(value), lock_battery_pct=100.0)

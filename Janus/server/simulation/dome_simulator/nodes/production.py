# dome_simulator/nodes/production.py
"""
Производство и материалы (разделы 3 и 7 реестра dome_sandbox_nodes.md):
printer_3d, cnc, material_inventory, equipment_cooling.

Принтер и фрезер сами берут задания в рабочее время (auto_jobs), чтобы
в песочнице было что наблюдать. Задание можно запустить и вручную:
действие start_job с value={"file_name": ..., "duration_s": ..., "filament_g": ...}
(duration_s — в секундах времени устройства; filament_g — расход филамента на деталь).

Д10: принтер — мощность нагревателей (heater_power_pct), калибровка оси X
(rotation_distance_x), расход филамента по заданию; ЧПУ — температура шпинделя
(spindle_temp_c) от контура охлаждения; склад — учёт катушки на принтере.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

from ..environment import OUProcess, clamp, relax
from ..events import Severity
from .base import register_node_type
from .sandbox import ControlError, SandboxNode, SensorNode, control, parse_number

ON, OFF, FAULT = "ON", "OFF", "FAULT"

_GCODE_RE = re.compile(r"^([GMT])(\d+)(.*)$", re.IGNORECASE)
_PARAM_RE = re.compile(r"([A-Z])\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def _gcode_lines(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        raise ControlError("send_gcode: ожидалась непустая строка G-code")
    lines = []
    for raw in value.replace(";", "\n;").splitlines():
        line = raw.split(";", 1)[0].strip()
        if line:
            lines.append(line.upper())
    return lines


def _gcode_params(rest: str) -> dict[str, float]:
    return {k.upper(): float(v) for k, v in _PARAM_RE.findall(rest)}


def _job_from_value(value: Any, default_duration_s: float) -> tuple[str, float]:
    if isinstance(value, str) and value.strip():
        return value.strip(), default_duration_s
    if isinstance(value, dict) and value.get("file_name"):
        duration = parse_number(value.get("duration_s", default_duration_s), 1.0, 7 * 86400.0,
                                "Длительность задания, с")
        return str(value["file_name"]), duration
    raise ControlError('start_job: ожидалось имя файла или {"file_name": ..., "duration_s": ...}')


def _filament_from_value(value: Any, duration_s: float) -> float:
    """Расход филамента на задание, г (по умолчанию ~12 г на час печати)."""
    if isinstance(value, dict) and value.get("filament_g") is not None:
        return parse_number(value["filament_g"], 0.0, 5000.0, "Расход филамента, г")
    return round(12.0 * duration_s / 3600.0, 1)


# ---------------------------------------------------------------------------
# 3.1 3D-принтер Anycubic Kobra 2 Pro
# ---------------------------------------------------------------------------

PRINT_JOBS = [
    ("bracket_v2.gcode", 2 * 3600),
    ("fan_duct.gcode", 1.5 * 3600),
    ("valve_handle.gcode", 3 * 3600),
    ("sensor_case.gcode", 4 * 3600),
    ("gear_m1_z24.gcode", 1 * 3600),
    ("cable_clip_x10.gcode", 0.75 * 3600),
]


@register_node_type("printer_3d")
class Printer3D(SandboxNode):
    """
    Входы для правил: control_powered (линия 3 щитка) — без питания принтер
    уходит offline, а текущая печать — в ERROR (POWER_LOSS); control_spool_g —
    остаток филамента на катушке (склад): при 0 — FILAMENT_RUNOUT и пауза (К13).

    webhook_state отстаёт от real_state на один тик — так ведёт себя
    webhook-сервис реального принтера.

    Потребление: нагрев стола 350 Вт + сопла 40 Вт, поддержание ~90 + 15 Вт,
    электроника/моторы 25 Вт. Пауза дольше 10 мин — расслоение корпуса
    (error_code LAYER_ADHESION, part_defect), №6.
    """
    title = "3D-принтер Anycubic Kobra 2 Pro"
    system = "Производство"
    category = "production"

    NOZZLE_PRINT_C, BED_PRINT_C, NOZZLE_STANDBY_C = 210.0, 60.0, 170.0
    ROTATION_DISTANCE_X = 40.0  # заводская калибровка (документация)
    ADHESION_PAUSE_S = 600.0

    def __init__(self, node_id: str, auto_jobs: bool = True, job_rate_per_hour: float = 0.4,
                 error_rate_per_hour: float = 0.02, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.auto_jobs = auto_jobs
        self.job_rate_per_hour = job_rate_per_hour
        self.error_rate_per_hour = error_rate_per_hour
        self._completed_age_s = 0.0
        self._paused_s = 0.0

    def initial_state(self) -> dict[str, Any]:
        return {
            "state": "IDLE",               # IDLE | PRINTING | PAUSED | ERROR | COMPLETED
            "paused": False,
            "nozzle_temp_c": 22.0,
            "nozzle_target_c": 0.0,
            "bed_temp_c": 22.0,
            "bed_target_c": 0.0,
            "file_name": None,
            "progress_pct": 0.0,
            "print_elapsed_s": 0.0,
            "print_duration_s": 0.0,
            "print_time_left_s": 0.0,
            "homed": False,
            "last_gcode": None,
            "gcode_state": "OK",           # OK | ERROR
            "gcode_error": None,
            "error_code": None,            # FILAMENT_RUNOUT | THERMAL_RUNAWAY | POWER_LOSS | EMERGENCY_STOP | LAYER_ADHESION
            "webhook_state": "IDLE",
            "real_state": "IDLE",
            "power_w": 8.0,
            "heater_power_pct": 0.0,       # мощность нагревателя сопла (extruder.power в Klipper)
            "bed_heater_power_pct": 0.0,
            "rotation_distance_x": self.ROTATION_DISTANCE_X,
            "filament_required_g": 0.0,
            "filament_used_g": 0.0,
            "part_defect": None,           # причина брака текущей детали
            "control_powered": True,
            "control_spool_g": None,
        }

    def _start(self, file_name: str, duration_s: float, filament_g: Optional[float] = None) -> dict[str, Any]:
        filament_g = round(12.0 * duration_s / 3600.0, 1) if filament_g is None else filament_g
        self.emit("printer.job_started", file_name=file_name, duration_s=duration_s, filament_g=filament_g)
        self._paused_s = 0.0
        return {
            "state": "PRINTING", "paused": False, "file_name": file_name,
            "progress_pct": 0.0, "print_elapsed_s": 0.0, "print_duration_s": duration_s,
            "print_time_left_s": duration_s, "error_code": None, "part_defect": None,
            "filament_required_g": filament_g, "filament_used_g": 0.0,
            "nozzle_target_c": self.NOZZLE_PRINT_C, "bed_target_c": self.BED_PRINT_C,
        }

    @staticmethod
    def _heat(temp: float, goal: float, ambient: float, rate_c_s: float, cool_tau_s: float, dt: float) -> float:
        """Нагреватель с ограниченной мощностью: к цели — не быстрее rate_c_s,
        остывание — экспоненциально к цели/окружению."""
        if goal > temp:
            return min(goal, temp + rate_c_s * dt)
        return relax(temp, goal, cool_tau_s, dt)

    def filament_rate_g_h(self, node: dict[str, Any]) -> float:
        """Текущий расход филамента (для правила «принтер → склад»)."""
        if node["state"] != "PRINTING" or node["print_duration_s"] <= 0:
            return 0.0
        heated = (node["nozzle_temp_c"] >= node["nozzle_target_c"] - 5.0
                  and node["bed_temp_c"] >= node["bed_target_c"] - 3.0)
        return node["filament_required_g"] / node["print_duration_s"] * 3600.0 if heated else 0.0

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        ambient = self.env.indoor.temperature_c
        u: dict[str, Any] = {}
        state = node["state"]

        if state == "PAUSED" and node["control_powered"]:
            self._paused_s += gdt
            if self._paused_s >= self.ADHESION_PAUSE_S and node["error_code"] != "LAYER_ADHESION":
                self.emit("printer.error", Severity.WARNING, error_code="LAYER_ADHESION",
                          file_name=node["file_name"], paused_s=round(self._paused_s))
                u.update(error_code="LAYER_ADHESION", part_defect="LAYER_ADHESION")
        elif state == "PRINTING":
            self._paused_s = 0.0

        if not node["control_powered"]:
            if state in ("PRINTING", "PAUSED"):
                self.emit("printer.error", Severity.CRITICAL, error_code="POWER_LOSS")
                u.update(state="ERROR", error_code="POWER_LOSS", paused=False)
            u.update(online=False, nozzle_target_c=0.0, bed_target_c=0.0, homed=False)
        else:
            u["online"] = True
            if state == "COMPLETED":
                self._completed_age_s += gdt
                if self._completed_age_s > 1800:  # деталь сняли со стола
                    u.update(state="IDLE", file_name=None, progress_pct=0.0)
            if (self.auto_jobs and state in ("IDLE", "COMPLETED") and 8 <= self.hour < 22
                    and self.happens(self.job_rate_per_hour, gdt)):
                file_name, duration = PRINT_JOBS[self.rng.randrange(len(PRINT_JOBS))]
                u.update(self._start(file_name, duration * self.rng.uniform(0.8, 1.2)))
            elif state == "PRINTING":
                u.update(self._advance(gdt, node))

        nozzle_target = u.get("nozzle_target_c", node["nozzle_target_c"])
        bed_target = u.get("bed_target_c", node["bed_target_c"])
        nozzle_goal = max(ambient, nozzle_target)
        bed_goal = max(ambient, bed_target)
        nozzle = self._heat(node["nozzle_temp_c"], nozzle_goal, ambient, 3.0, 60.0, gdt)
        bed = self._heat(node["bed_temp_c"], bed_goal, ambient, 0.3, 300.0, gdt)
        u["nozzle_temp_c"] = round(nozzle + self.noise(0.4), 1)
        u["bed_temp_c"] = round(bed + self.noise(0.2), 1)

        # Нагреватели: полная мощность при нагреве, поддержание — доля мощности
        powered = node["control_powered"]
        nozzle_pct = 0.0
        if powered and nozzle_target > 0:
            nozzle_pct = 100.0 if nozzle < nozzle_target - 3.0 else 30.0 + self.noise(2.0)
        bed_pct = 0.0
        if powered and bed_target > 0:
            bed_pct = 100.0 if bed < bed_target - 2.0 else 26.0 + self.noise(2.0)
        busy = u.get("state", state) in ("PRINTING", "PAUSED")
        power = (25.0 if busy else 8.0) + 350.0 * bed_pct / 100.0 + 40.0 * nozzle_pct / 100.0
        u["heater_power_pct"] = round(clamp(nozzle_pct, 0.0, 100.0), 1)
        u["bed_heater_power_pct"] = round(clamp(bed_pct, 0.0, 100.0), 1)
        u["power_w"] = round(power if powered else 0.0, 1)

        u["webhook_state"] = node["real_state"]
        u["real_state"] = u.get("state", state)
        return u

    def _advance(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        if self.happens(self.error_rate_per_hour, gdt):
            code = "THERMAL_RUNAWAY" if self.rng.random() < 0.15 else "FILAMENT_RUNOUT"
            self.emit("printer.error", Severity.WARNING, error_code=code, file_name=node["file_name"])
            return {"state": "ERROR", "error_code": code, "nozzle_target_c": 0.0, "bed_target_c": 0.0}

        heated = (node["nozzle_temp_c"] >= node["nozzle_target_c"] - 5.0
                  and node["bed_temp_c"] >= node["bed_target_c"] - 3.0)
        spool = node.get("control_spool_g")
        if heated and spool is not None and float(spool) <= 0.0:
            # катушка кончилась — Klipper ставит печать на паузу по датчику филамента
            self.emit("printer.error", Severity.WARNING, error_code="FILAMENT_RUNOUT", file_name=node["file_name"],
                      progress_pct=node["progress_pct"])
            return {"state": "PAUSED", "paused": True, "error_code": "FILAMENT_RUNOUT",
                    "nozzle_target_c": self.NOZZLE_STANDBY_C}
        elapsed = node["print_elapsed_s"] + (gdt if heated else 0.0)
        duration = node["print_duration_s"]
        used = node.get("filament_used_g", 0.0) + (node["filament_required_g"] * gdt / duration if heated else 0.0)
        if elapsed >= duration:
            self._completed_age_s = 0.0
            self.emit("printer.job_done", file_name=node["file_name"], part_defect=node.get("part_defect"))
            return {"state": "COMPLETED", "progress_pct": 100.0, "print_elapsed_s": duration,
                    "print_time_left_s": 0.0, "nozzle_target_c": 0.0, "bed_target_c": 0.0,
                    "filament_used_g": round(node["filament_required_g"], 1)}
        return {"print_elapsed_s": round(elapsed, 1),
                "progress_pct": round(elapsed / duration * 100.0, 1),
                "print_time_left_s": round(duration - elapsed, 0),
                "filament_used_g": round(used, 2)}

    # ---- управление ----

    def _require_online(self, node: dict[str, Any]) -> None:
        if not node["online"]:
            raise ControlError("Принтер offline")

    @control("pause")
    def _pause(self, node, value):
        self._require_online(node)
        if node["state"] != "PRINTING":
            raise ControlError(f"Пауза невозможна в состоянии {node['state']}")
        return {"state": "PAUSED", "paused": True, "nozzle_target_c": self.NOZZLE_STANDBY_C}

    @control("resume")
    def _resume(self, node, value):
        self._require_online(node)
        if node["state"] != "PAUSED":
            raise ControlError(f"Продолжение невозможно в состоянии {node['state']}")
        spool = node.get("control_spool_g")
        if node["error_code"] == "FILAMENT_RUNOUT" and spool is not None and float(spool) <= 0.0:
            raise ControlError("Филамент закончился: сначала замените катушку")
        error = node["error_code"] if node["error_code"] == "LAYER_ADHESION" else None
        return {"state": "PRINTING", "paused": False, "nozzle_target_c": self.NOZZLE_PRINT_C, "error_code": error}

    @control("abort", "stop", "cancel_job")
    def _abort(self, node, value):
        if node["state"] not in ("PRINTING", "PAUSED", "ERROR"):
            raise ControlError(f"Нечего прерывать в состоянии {node['state']}")
        return {"state": "IDLE", "paused": False, "file_name": None, "progress_pct": 0.0,
                "print_elapsed_s": 0.0, "print_duration_s": 0.0, "print_time_left_s": 0.0,
                "error_code": None, "nozzle_target_c": 0.0, "bed_target_c": 0.0,
                "filament_required_g": 0.0, "filament_used_g": 0.0, "part_defect": None}

    @control("home")
    def _home(self, node, value):
        self._require_online(node)
        if node["state"] in ("PRINTING", "PAUSED"):
            raise ControlError("HOME недоступен во время печати")
        return {"homed": True}

    @control("start_job", "start_print")
    def _start_job(self, node, value):
        self._require_online(node)
        if node["state"] not in ("IDLE", "COMPLETED"):
            raise ControlError(f"Принтер занят (состояние {node['state']})")
        file_name, duration = _job_from_value(value, 2 * 3600.0)
        return self._start(file_name, duration, _filament_from_value(value, duration))

    @control("send_gcode", "gcode")
    def _send_gcode(self, node, value):
        self._require_online(node)
        lines = _gcode_lines(value)
        u: dict[str, Any] = {"last_gcode": "\n".join(lines), "gcode_state": "OK", "gcode_error": None}
        state = node["state"]
        for line in lines:
            m = _GCODE_RE.match(line)
            if not m:
                u.update(gcode_state="ERROR", gcode_error=f"Unknown command: {line}")
                break
            cmd = f"{m.group(1)}{int(m.group(2))}"
            params = _gcode_params(m.group(3))
            if cmd == "G28":
                if state in ("PRINTING", "PAUSED"):
                    u.update(gcode_state="ERROR", gcode_error="G28 rejected: printer busy")
                    break
                u["homed"] = True
            elif cmd in ("M104", "M109"):
                u["nozzle_target_c"] = clamp(params.get("S", 0.0), 0.0, 260.0)
            elif cmd in ("M140", "M190"):
                u["bed_target_c"] = clamp(params.get("S", 0.0), 0.0, 110.0)
            elif cmd == "M25" and state == "PRINTING":
                u.update(self._pause({**node, **u, "state": state}, None))
                state = "PAUSED"
            elif cmd == "M24" and state == "PAUSED":
                u.update(self._resume({**node, **u, "state": state}, None))
                state = "PRINTING"
            elif cmd == "M524" and state in ("PRINTING", "PAUSED"):
                u.update(self._abort({**node, **u, "state": state}, None))
                state = "IDLE"
            elif cmd == "M112":
                self.emit("printer.error", Severity.CRITICAL, error_code="EMERGENCY_STOP")
                u.update(state="ERROR", error_code="EMERGENCY_STOP", nozzle_target_c=0.0, bed_target_c=0.0)
                state = "ERROR"
            elif cmd.startswith("G") and int(m.group(2)) in (0, 1, 90, 91, 92):
                pass  # перемещения и режимы координат принимаются без эффекта на телеметрию
            elif cmd in ("M105", "M114", "M115", "M106", "M107", "M84", "M18", "M220", "M221"):
                pass
            else:
                u.update(gcode_state="ERROR", gcode_error=f"Unsupported command: {cmd}")
                break
        return u


# ---------------------------------------------------------------------------
# 3.2 ЧПУ-фрезер 3018 Pro (GRBL)
# ---------------------------------------------------------------------------

CNC_JOBS = [
    ("pcb_power_board.nc", 1.2 * 3600),
    ("pcb_sensor_hub.nc", 0.8 * 3600),
    ("pcb_relay_driver.nc", 1.5 * 3600),
    ("front_panel_engrave.nc", 0.5 * 3600),
]

GRBL_ALARMS = {
    "ALARM:1": "Hard limit triggered",
    "ALARM:2": "Soft limit: target exceeds machine travel",
    "ALARM:5": "Homing fail: limit switch not found",
}


@register_node_type("cnc")
class CNCMill(SandboxNode):
    """
    Координаты — в миллиметрах рабочей области 300×180×45.
    Входы для правил: control_powered (линия 2 щитка), control_coolant_temp_c
    (температура жидкости контура охлаждения → spindle_temp_c).

    Без питания GRBL сбрасывается: ALARM:3, позиция потеряна (homed=False).
    FeedHold останавливает шпиндель (парковка), Resume — раскручивает снова.
    Качество: spindle_temp_c > 40 °C — DEGRADED, > 45 °C — OVERHEAT (брак, К7).
    """
    title = "ЧПУ-фрезер 3018 Pro"
    system = "Производство"
    category = "production"

    TRAVEL = (300.0, 180.0, 45.0)
    SPINDLE_RPM = 10000.0
    SPINDLE_W, STEPPERS_W, IDLE_W = 250.0, 40.0, 25.0

    def __init__(self, node_id: str, auto_jobs: bool = True, job_rate_per_hour: float = 0.25,
                 alarm_rate_per_hour: float = 0.02, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.auto_jobs = auto_jobs
        self.job_rate_per_hour = job_rate_per_hour
        self.alarm_rate_per_hour = alarm_rate_per_hour

    def initial_state(self) -> dict[str, Any]:
        return {
            "state": "IDLE",               # IDLE | RUNNING | PAUSED | ALARM
            "paused": False,
            "x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0,
            "spindle_rpm": 0.0,
            "spindle_target_rpm": 0.0,
            "file_name": None,
            "progress_pct": 0.0,
            "job_elapsed_s": 0.0,
            "job_duration_s": 0.0,
            "job_time_left_s": 0.0,
            "homed": False,
            "alarm": False,
            "alarm_code": None,
            "error_code": None,            # последний ответ GRBL error:N на команду
            "error_text": None,
            "last_gcode": None,
            "power_w": self.IDLE_W,
            "spindle_temp_c": 22.0,
            "quality_state": "OK",         # OK | DEGRADED | OVERHEAT
            "part_defect": None,
            "control_powered": True,
            "control_coolant_temp_c": None,
        }

    def _start(self, file_name: str, duration_s: float) -> dict[str, Any]:
        self.emit("cnc.job_started", file_name=file_name, duration_s=duration_s)
        return {"state": "RUNNING", "paused": False, "file_name": file_name, "progress_pct": 0.0,
                "job_elapsed_s": 0.0, "job_duration_s": duration_s, "job_time_left_s": duration_s,
                "spindle_target_rpm": self.SPINDLE_RPM, "part_defect": None}

    def _toolpath(self, t: float) -> tuple[float, float, float]:
        """Правдоподобная траектория фрезеровки платы: обход контуров + редкие холостые ходы."""
        x = 150.0 + 110.0 * math.sin(2 * math.pi * t / 640.0)
        y = 90.0 + 70.0 * math.sin(2 * math.pi * t / 410.0 + 1.1)
        z = 5.0 if (t % 300.0) < 15.0 else -1.6  # подъём на безопасную высоту
        return x, y, z

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        u: dict[str, Any] = {}
        state = node["state"]

        if not node["control_powered"]:
            u.update(online=False, spindle_target_rpm=0.0, homed=False)
            if state in ("RUNNING", "PAUSED"):
                u.update(state="ALARM", alarm=True, alarm_code="ALARM:3", paused=False)
                self.emit("cnc.alarm", Severity.CRITICAL, alarm_code="ALARM:3", reason="power_loss")
        else:
            u["online"] = True
            if (self.auto_jobs and state == "IDLE" and 9 <= self.hour < 19 and self.happens(self.job_rate_per_hour, gdt)):
                file_name, duration = CNC_JOBS[self.rng.randrange(len(CNC_JOBS))]
                u.update(self._start(file_name, duration * self.rng.uniform(0.85, 1.15)))
            elif state == "RUNNING":
                u.update(self._advance(gdt, node))

        target = u.get("spindle_target_rpm", node["spindle_target_rpm"])
        rpm = relax(node["spindle_rpm"], target, 5, gdt)
        u["spindle_rpm"] = round(rpm + (self.noise(60.0) if rpm > 100 else 0.0), 0) if rpm > 1 else 0.0

        spinning = rpm > 500
        running = u.get("state", state) == "RUNNING"
        if node["control_powered"]:
            u["power_w"] = round(self.IDLE_W + (self.SPINDLE_W * rpm / self.SPINDLE_RPM if spinning else 0.0)
                                 + (self.STEPPERS_W if running else 0.0), 1)
        else:
            u["power_w"] = 0.0
        coolant = node.get("control_coolant_temp_c")
        coolant = self.env.indoor.temperature_c + 6.0 if coolant is None else float(coolant)
        spindle_t = relax(node["spindle_temp_c"], coolant + (4.0 if spinning else 0.0), 90.0, gdt)
        u["spindle_temp_c"] = round(spindle_t + self.noise(0.1), 1)
        quality = "OVERHEAT" if spindle_t > 45.0 else ("DEGRADED" if spindle_t > 40.0 else "OK")
        if quality != node["quality_state"] and quality != "OK":
            self.emit("cnc.spindle_temperature", Severity.WARNING if quality == "DEGRADED" else Severity.CRITICAL,
                      quality_state=quality, spindle_temp_c=round(spindle_t, 1))
        u["quality_state"] = quality
        if running and quality == "OVERHEAT" and not node.get("part_defect"):
            u["part_defect"] = "SPINDLE_OVERHEAT"
        return u

    def _advance(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        if self.happens(self.alarm_rate_per_hour, gdt):
            code = self.rng.choice(["ALARM:1", "ALARM:2"])
            self.emit("cnc.alarm", Severity.WARNING, alarm_code=code, file_name=node["file_name"])
            return {"state": "ALARM", "alarm": True, "alarm_code": code, "spindle_target_rpm": 0.0,
                    "homed": False}
        elapsed = node["job_elapsed_s"] + gdt
        duration = node["job_duration_s"]
        if elapsed >= duration:
            self.emit("cnc.job_done", file_name=node["file_name"])
            return {"state": "IDLE", "progress_pct": 100.0, "job_elapsed_s": duration, "job_time_left_s": 0.0,
                    "spindle_target_rpm": 0.0, "z_mm": 5.0}
        x, y, z = self._toolpath(elapsed)
        return {"job_elapsed_s": round(elapsed, 1), "progress_pct": round(elapsed / duration * 100.0, 1),
                "job_time_left_s": round(duration - elapsed, 0),
                "x_mm": round(x, 3), "y_mm": round(y, 3), "z_mm": round(z, 3)}

    # ---- управление ----

    def _require_ready(self, node: dict[str, Any]) -> None:
        if not node["online"]:
            raise ControlError("Фрезер offline")

    @control("feed_hold", "feedhold", "pause", "!")
    def _feed_hold(self, node, value):
        self._require_ready(node)
        if node["state"] != "RUNNING":
            raise ControlError(f"FeedHold невозможен в состоянии {node['state']}")
        return {"state": "PAUSED", "paused": True, "spindle_target_rpm": 0.0}

    @control("resume", "cycle_start", "~")
    def _resume(self, node, value):
        self._require_ready(node)
        if node["state"] != "PAUSED":
            raise ControlError(f"Resume невозможен в состоянии {node['state']}")
        spindle = self.SPINDLE_RPM if node["file_name"] else node["spindle_target_rpm"]
        return {"state": "RUNNING", "paused": False, "spindle_target_rpm": spindle}

    @control("abort", "stop", "cancel_job", "soft_reset")
    def _abort(self, node, value):
        if node["state"] not in ("RUNNING", "PAUSED"):
            raise ControlError(f"Нечего прерывать в состоянии {node['state']}")
        return {"state": "IDLE", "paused": False, "file_name": None, "progress_pct": 0.0,
                "job_elapsed_s": 0.0, "job_duration_s": 0.0, "job_time_left_s": 0.0,
                "spindle_target_rpm": 0.0}

    @control("home", "$h")
    def _home(self, node, value):
        self._require_ready(node)
        if node["state"] in ("RUNNING", "PAUSED"):
            raise ControlError("Home недоступен во время обработки")
        return {"state": "IDLE", "alarm": False, "alarm_code": None, "homed": True,
                "x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0}

    @control("reset_alarm", "unlock", "$x")
    def _reset_alarm(self, node, value):
        if node["state"] != "ALARM":
            raise ControlError("Фрезер не в состоянии ALARM")
        return {"state": "IDLE", "alarm": False, "alarm_code": None, "paused": False}

    @control("start_job")
    def _start_job(self, node, value):
        self._require_ready(node)
        if node["state"] != "IDLE":
            raise ControlError(f"Фрезер занят (состояние {node['state']})")
        return self._start(*_job_from_value(value, 3600.0))

    @control("send_gcode", "gcode")
    def _send_gcode(self, node, value):
        self._require_ready(node)
        lines = _gcode_lines(value)
        u: dict[str, Any] = {"last_gcode": "\n".join(lines), "error_code": None, "error_text": None}
        state = node["state"]
        pos = [node["x_mm"], node["y_mm"], node["z_mm"]]
        for line in lines:
            if line in ("$H", "$X", "!", "~"):
                handler = {"$H": self._home, "$X": self._reset_alarm, "!": self._feed_hold, "~": self._resume}[line]
                u.update(handler({**node, **u, "state": state}, None))
                state = u.get("state", state)
                continue
            if state == "ALARM":
                u.update(error_code="error:9", error_text="G-code locked out during alarm")
                break
            m = _GCODE_RE.match(line)
            if not m:
                u.update(error_code="error:20", error_text=f"Unsupported command: {line}")
                break
            cmd = f"{m.group(1)}{int(m.group(2))}"
            params = _gcode_params(m.group(3))
            if cmd in ("G0", "G1"):
                if state == "RUNNING":
                    u.update(error_code="error:8", error_text="Command requires idle state")
                    break
                for axis, i in (("X", 0), ("Y", 1), ("Z", 2)):
                    if axis in params:
                        pos[i] = params[axis]
                if not all(0.0 <= p <= t for p, t in zip(pos[:2], self.TRAVEL[:2])) or not -self.TRAVEL[2] <= pos[2] <= 10.0:
                    self.emit("cnc.alarm", Severity.WARNING, alarm_code="ALARM:2")
                    u.update(state="ALARM", alarm=True, alarm_code="ALARM:2", spindle_target_rpm=0.0)
                    break
                u.update(x_mm=pos[0], y_mm=pos[1], z_mm=pos[2])
            elif cmd in ("M3", "M4"):
                u["spindle_target_rpm"] = clamp(params.get("S", self.SPINDLE_RPM), 0.0, 12000.0)
            elif cmd == "M5":
                u["spindle_target_rpm"] = 0.0
            elif cmd in ("G20", "G21", "G90", "G91", "G92", "G54", "G17", "M8", "M9", "G4"):
                pass
            else:
                u.update(error_code="error:20", error_text=f"Unsupported command: {cmd}")
                break
        return u


# ---------------------------------------------------------------------------
# 7.1 Склад материалов
# ---------------------------------------------------------------------------

@register_node_type("material_inventory")
class MaterialInventory(SensorNode):
    """
    Истинные остатки хранятся в узле; наружу — показания весов/датчиков с шумом.
    filament_g — филамент на катушке, установленной в принтер (весы держателя);
    filament_pct — от полной катушки. spare_spools — запасные катушки.
    Входы для правил (расход в час): control_filament_usage_g_h (от принтера),
    control_blanks_usage_per_h, control_consumables_usage_pct_h.
    Замена катушки — физическое действие Оператора replace_spool (К13).
    """
    title = "Склад материалов"
    system = "Производство"
    category = "production"

    def __init__(self, node_id: str, filament_capacity_g: float = 1000.0, filament_g: float = 850.0,
                 spare_spools: int = 3, cnc_blanks: int = 30, consumables_pct: float = 85.0,
                 glitch_rate_per_hour: float = 0.01, seed: Optional[int] = None) -> None:
        super().__init__(node_id, glitch_rate_per_hour, seed)
        self.filament_capacity_g = filament_capacity_g
        self._filament_g = filament_g
        self._spare_spools = spare_spools
        self._blanks = float(cnc_blanks)
        self._consumables_pct = consumables_pct

    @property
    def true_filament_g(self) -> float:
        return self._filament_g

    def set_spool(self, grams: float) -> None:
        """Начальные условия сценария: неполная катушка (К13)."""
        self._filament_g = max(0.0, float(grams))

    def initial_state(self) -> dict[str, Any]:
        return {
            "filament_g": self._filament_g,
            "filament_pct": round(self._filament_g / self.filament_capacity_g * 100, 1),
            "spool_capacity_g": self.filament_capacity_g,
            "spare_spools": self._spare_spools,
            "cnc_blanks_count": int(self._blanks),
            "consumables_pct": self._consumables_pct,
            "low_stock_warnings": [],
            "control_filament_usage_g_h": 0.0,
            "control_blanks_usage_per_h": 0.0,
            "control_consumables_usage_pct_h": 0.0,
        }

    @control("replace_spool", physical=True)
    def _replace_spool(self, node, value):
        """Оператор ставит новую катушку филамента."""
        if node["spare_spools"] <= 0:
            raise ControlError("Запасных катушек нет")
        self._filament_g = self.filament_capacity_g
        self.emit("inventory.spool_replaced", Severity.INFO)
        return {"filament_g": self._filament_g, "filament_pct": 100.0, "spare_spools": node["spare_spools"] - 1}

    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> dict[str, Any]:
        hours = gdt / 3600.0
        self._filament_g = max(0.0, self._filament_g - float(node["control_filament_usage_g_h"] or 0) * hours)
        self._blanks = max(0.0, self._blanks - float(node["control_blanks_usage_per_h"] or 0) * hours)
        self._consumables_pct = max(0.0, self._consumables_pct
                                    - float(node["control_consumables_usage_pct_h"] or 0) * hours)

        filament = max(0.0, self._filament_g + self.noise(3.0 * noise_k))
        filament_pct = filament / self.filament_capacity_g * 100
        warnings = []
        if filament_pct < 15:
            warnings.append("FILAMENT_LOW")
        if self._blanks < 5:
            warnings.append("CNC_BLANKS_LOW")
        if self._consumables_pct < 20:
            warnings.append("CONSUMABLES_LOW")
        new = set(warnings) - set(node["low_stock_warnings"])
        if new:
            self.emit("inventory.low_stock", Severity.WARNING, items=sorted(new))
        return {
            "filament_g": round(filament, 0),
            "filament_pct": round(filament_pct, 1),
            "cnc_blanks_count": int(self._blanks),
            "consumables_pct": round(self._consumables_pct + self.noise(0.2 * noise_k), 1),
            "low_stock_warnings": warnings,
        }


# ---------------------------------------------------------------------------
# 7.2 Охлаждение оборудования
# ---------------------------------------------------------------------------

@register_node_type("equipment_cooling")
class EquipmentCooling(SandboxNode):
    """
    Жидкостный контур охлаждения ЧПУ и 3D-принтера.
    Вход для правил: control_heat_load_w — тепловая нагрузка от оборудования
    (None — типовой профиль рабочего дня).
    Кризисный вход: control_air_in_loop — воздушная пробка, расход падает до 25 %
    (К7); устраняется прокачкой контура bleed_loop (физически, ~1 мин).
    Перегрев жидкости (> 45 °C) — авария OVERHEAT, насос продолжает работать.
    """
    title = "Охлаждение / чиллер для ЧПУ и 3D-принтера"
    system = "Производство"
    category = "production"

    AIR_FLOW_K = 0.25
    BLEED_S = 60.0

    def __init__(self, node_id: str, nominal_flow_l_min: float = 6.0, pump_rated_w: float = 60.0,
                 fault_rate_per_hour: float = 0.003, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.nominal_flow_l_min = nominal_flow_l_min
        self.pump_rated_w = pump_rated_w
        self.fault_rate_per_hour = fault_rate_per_hour
        self._heat = OUProcess(1.0, 0.2, 900, self.rng)
        self._bleed_s = 0.0

    def initial_state(self) -> dict[str, Any]:
        return {
            "coolant_temp_c": 22.0,
            "coolant_flow_l_min": self.nominal_flow_l_min * 0.7,
            "pump_power_w": self.pump_rated_w * 0.7 ** 3,
            "heat_exchanger_temp_c": 22.0,
            "speed_pct": 70.0,
            "state": ON,
            "alarm": False,
            "alarm_code": None,            # OVERHEAT | PUMP_FAULT
            "bleeding": False,
            "control_heat_load_w": None,
            "control_air_in_loop": False,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        ambient = self.env.indoor.temperature_c
        state, alarm_code = node["state"], node["alarm_code"]

        if node["control_heat_load_w"] is not None:
            heat_w = float(node["control_heat_load_w"])
        else:
            heat_w = (350.0 if 9 <= self.hour < 19 else 40.0) * clamp(self._heat.step(gdt), 0.2, 2.0)

        if state == ON and self.happens(self.fault_rate_per_hour, gdt):
            state, alarm_code = FAULT, "PUMP_FAULT"
            self.emit("cooling.alarm", Severity.WARNING, alarm_code=alarm_code)

        u: dict[str, Any] = {}
        if self._bleed_s > 0:
            self._bleed_s -= gdt
            if self._bleed_s <= 0:
                u.update(bleeding=False, control_air_in_loop=False)
                self.emit("cooling.bled", Severity.INFO)
        air = bool(u.get("control_air_in_loop", node["control_air_in_loop"]))

        running = state == ON
        speed = node["speed_pct"] / 100.0 if running else 0.0
        flow = self.nominal_flow_l_min * speed * (1.0 + self.noise(0.02)) if running else 0.0
        if air and running:
            flow *= self.AIR_FLOW_K * (0.8 + 0.4 * speed)   # пробка: насос гонит воздух
        pump_w = self.pump_rated_w * speed ** 3 + (self.noise(0.5) if running else 0.0)

        # Установившийся перегрев жидкости: ΔT = Q / (G·c) плюс теплообменник с воздухом
        cooling_capacity = 2.0 + 12.0 * flow  # Вт/°C
        target = ambient + heat_w / cooling_capacity
        coolant = relax(node["coolant_temp_c"], target, 600, gdt) + self.noise(0.1)
        exchanger = ambient + (coolant - ambient) * 0.6 + self.noise(0.1)

        if coolant > 45.0 and alarm_code is None:
            alarm_code = "OVERHEAT"
            self.emit("cooling.alarm", Severity.CRITICAL, alarm_code=alarm_code, coolant_temp_c=round(coolant, 1))

        u.update({
            "coolant_temp_c": round(coolant, 1),
            "coolant_flow_l_min": round(max(0.0, flow), 2),
            "pump_power_w": round(max(0.0, pump_w), 1),
            "heat_exchanger_temp_c": round(exchanger, 1),
            "state": state,
            "alarm": alarm_code is not None,
            "alarm_code": alarm_code,
        })
        return u

    @control("bleed_loop", physical=True)
    def _bleed(self, node, value):
        """Прокачка контура — удаление воздуха (Оператор, ~1 мин)."""
        self._bleed_s = self.BLEED_S
        return {"bleeding": True}

    @control("turn_on", "on", "start", "enable")
    def _turn_on(self, node, value):
        if node["state"] == FAULT:
            raise ControlError("Авария контура: сначала выполните reset_alarm")
        return {"state": ON}

    @control("turn_off", "off", "stop", "disable")
    def _turn_off(self, node, value):
        return {"state": OFF}  # авария (если была) остаётся до reset_alarm

    @control("set_speed")
    def _set_speed(self, node, value):
        return {"speed_pct": parse_number(value, 0.0, 100.0, "Скорость насоса, %")}

    @control("reset_alarm")
    def _reset_alarm(self, node, value):
        return {"state": ON if node["state"] == FAULT else node["state"], "alarm": False, "alarm_code": None}

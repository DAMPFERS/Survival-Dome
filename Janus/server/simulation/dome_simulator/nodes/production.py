# dome_simulator/nodes/production.py
"""
Производство и материалы (разделы 3 и 7 реестра dome_sandbox_nodes.md):
printer_3d, cnc, material_inventory, equipment_cooling.

Принтер и фрезер сами берут задания в рабочее время (auto_jobs), чтобы
в песочнице было что наблюдать. Задание можно запустить и вручную:
действие start_job с именем файла из списка files устройства. Служебный путь
сценариев: value={"file_name": ..., "duration_s": ..., "filament_g": ...} — с явным
duration_s файл может отсутствовать в списке (duration_s — в секундах времени
устройства; filament_g — расход филамента на деталь).

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
from ..store import StateStore
from .base import register_node_type
from .sandbox import ControlError, SandboxNode, SensorNode, control, parse_number, query

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


PRINTER_EXTENSIONS = (".gcode", ".g", ".gco")
CNC_EXTENSIONS = (".gcode", ".nc", ".tap", ".ngc", ".gc", ".cnc")
MAX_UPLOAD_BYTES = 16 * 1024 * 1024

_TIME_RE = re.compile(r"^;\s*(?:estimated printing time[^=]*=\s*(.+)|TIME:\s*(\d+))\s*$", re.IGNORECASE | re.MULTILINE)
_FILAMENT_G_RE = re.compile(r"^;\s*(?:total )?filament used \[g\]\s*=\s*([\d.]+)", re.IGNORECASE | re.MULTILINE)
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])", re.IGNORECASE)


def file_name_from_value(value: Any, extensions: tuple[str, ...], what: str) -> str:
    """Имя файла без пути (защита от выхода из каталога файлов устройства)."""
    name = value.get("file_name") if isinstance(value, dict) else value
    if not isinstance(name, str) or not name.strip():
        raise ControlError(f"{what}: ожидалось имя файла")
    name = name.strip()
    if "/" in name or "\\" in name or name.startswith(".") or len(name) > 200:
        raise ControlError(f"{what}: допускается только имя файла без пути")
    if not name.lower().endswith(extensions):
        raise ControlError(f"{what}: расширение файла должно быть одним из {list(extensions)}")
    return name


def gcode_estimate(content: str) -> tuple[Optional[float], Optional[float]]:
    """Время печати, с, и расход филамента, г, из комментариев слайсера (Orca/Prusa/Cura)."""
    duration = None
    m = _TIME_RE.search(content)
    if m and m.group(2):
        duration = float(m.group(2))
    elif m:
        mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        duration = sum(float(v) * mult[u.lower()] for v, u in _DURATION_PART_RE.findall(m.group(1))) or None
    g = _FILAMENT_G_RE.search(content)
    return duration, (float(g.group(1)) if g else None)


def _job_file(files: dict[str, dict[str, Any]], value: Any, default_duration_s: float,
              what: str) -> tuple[str, float, Optional[dict[str, Any]]]:
    """Задание по файлу из памяти устройства. Явный duration_s — служебный путь
    сценариев и тестов: файл может отсутствовать в списке."""
    file_name, duration = _job_from_value(value, default_duration_s)
    explicit = isinstance(value, dict) and value.get("duration_s") is not None
    meta = files.get(file_name)
    if meta is None and not explicit:
        raise ControlError(f"{what}: файл {file_name!r} не найден. Доступно: {sorted(files)}")
    if meta is not None and not explicit and meta.get("estimated_time_s"):
        duration = float(meta["estimated_time_s"])
    return file_name, duration, meta


def _file_list(files: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": name, **meta} for name, meta in sorted(files.items())]


# Удержание станка сервером (holds.py): поля телеметрии и их сброс
HOLD_STATE = {"hold_level": None, "hold_reason": None, "resume_attempts": 0, "lockout_remaining_s": 0.0}


def _hold_reason(value: Any) -> str:
    reason = value.get("reason") if isinstance(value, dict) else value
    if not isinstance(reason, str) or not reason.strip():
        raise ControlError('server_hold: ожидалось {"reason": "<код причины>"}')
    return reason.strip().upper()


def _hold_updates(node: dict[str, Any], reason: str) -> dict[str, Any]:
    """Новое удержание — WARNING с причиной; уже удерживаемый станок сохраняет первую причину."""
    if node.get("hold_level"):
        return {}
    return {"hold_level": "WARNING", "hold_reason": reason, "resume_attempts": 0, "lockout_remaining_s": 0.0}


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

    Файлы печати: files — имена файлов в памяти принтера (запрос list_files —
    с размером и оценкой времени печати); upload_file {"file_name", "content"},
    delete_file, start_job по имени файла. Время печати и расход филамента
    берутся из комментариев слайсера. set_speed — скорость печати 50..200 %
    (M220), move — перемещение в относительных/абсолютных координатах
    (только после HOME, не во время печати).

    Потребление: нагрев стола 350 Вт + сопла 40 Вт, поддержание ~90 + 15 Вт,
    электроника/моторы 25 Вт. Пауза дольше 10 мин — расслоение корпуса
    (error_code LAYER_ADHESION, part_defect), №6.

    Удержание сервером (holds.py): server_hold {"reason"} — пауза по интерлоку или
    кризису (hold_level, hold_reason, resume_attempts, lockout_remaining_s);
    release_hold — снятие удержания админом. ИИ-агенту пауза и отмена недоступны
    (политика доступа), resume проверяет причину удержания.
    bed_clear — стол свободен; после завершения печати false, пока Оператор не
    заберёт деталь (дверь бокса ФабЛаб или remove_part).
    """
    title = "3D-принтер Anycubic Kobra 2 Pro"
    system = "Производство"
    category = "production"

    NOZZLE_PRINT_C, BED_PRINT_C, NOZZLE_STANDBY_C = 210.0, 60.0, 170.0
    ROTATION_DISTANCE_X = 40.0  # заводская калибровка (документация)
    ADHESION_PAUSE_S = 600.0
    TRAVEL = (220.0, 220.0, 250.0)   # область печати Kobra 2 Pro, мм
    MAX_RELATIVE_MOVE = 50.0         # ограничение драйвера (klipper_printer.move_relative)

    def __init__(self, node_id: str, auto_jobs: bool = True, job_rate_per_hour: float = 0.4,
                 error_rate_per_hour: float = 0.02, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.auto_jobs = auto_jobs
        self.job_rate_per_hour = job_rate_per_hour
        self.error_rate_per_hour = error_rate_per_hour
        self._completed_age_s = 0.0
        self._paused_s = 0.0
        # файлы в памяти принтера: имя -> {size_bytes, estimated_time_s, filament_g}
        self._files: dict[str, dict[str, Any]] = {
            name: {"size_bytes": int(duration * 55), "estimated_time_s": float(duration),
                   "filament_g": round(12.0 * duration / 3600.0, 1)}
            for name, duration in PRINT_JOBS}

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
            "x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0,
            "speed_pct": 100.0,            # множитель скорости печати (M220), %
            "files": sorted(self._files),  # файлы печати в памяти принтера
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
            **HOLD_STATE,
            "bed_clear": True,             # стол свободен (деталь снята)
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
        speed = float(node.get("speed_pct") or 100.0) / 100.0
        return node["filament_required_g"] / node["print_duration_s"] * 3600.0 * speed if heated else 0.0

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
                if self._completed_age_s > 1800 and self.auto_jobs:  # деталь сняли со стола
                    u.update(state="IDLE", file_name=None, progress_pct=0.0, bed_clear=True)
            if (self.auto_jobs and self._files and state in ("IDLE", "COMPLETED") and 8 <= self.hour < 22
                    and self.happens(self.job_rate_per_hour, gdt)):
                names = sorted(self._files)
                file_name = names[self.rng.randrange(len(names))]
                meta = self._files[file_name]
                u.update(self._start(file_name, meta["estimated_time_s"] * self.rng.uniform(0.8, 1.2),
                                     meta.get("filament_g")))
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
        # print_elapsed_s — пройденная часть задания в секундах при скорости 100 %
        step = gdt * float(node.get("speed_pct") or 100.0) / 100.0 if heated else 0.0
        elapsed = node["print_elapsed_s"] + step
        duration = node["print_duration_s"]
        used = node.get("filament_used_g", 0.0) + node["filament_required_g"] * step / duration
        if elapsed >= duration:
            self._completed_age_s = 0.0
            self.emit("printer.job_done", file_name=node["file_name"], part_defect=node.get("part_defect"))
            return {"state": "COMPLETED", "progress_pct": 100.0, "print_elapsed_s": duration,
                    "print_time_left_s": 0.0, "nozzle_target_c": 0.0, "bed_target_c": 0.0,
                    "filament_used_g": round(node["filament_required_g"], 1)}
        u = {"print_elapsed_s": round(elapsed, 1),
             "progress_pct": round(elapsed / duration * 100.0, 1),
             "print_time_left_s": round(duration - elapsed, 0),
             "filament_used_g": round(used, 2)}
        if heated:  # сопло обходит контуры слоя, Z растёт с прогрессом
            u.update(x_mm=round(110.0 + 40.0 * math.sin(elapsed / 7.0), 2),
                     y_mm=round(110.0 + 40.0 * math.cos(elapsed / 11.0), 2),
                     z_mm=round(0.2 + 30.0 * elapsed / duration, 2))
        return u

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

    @control("server_hold")
    def _server_hold(self, node, value):
        """Удержание сервером: {"reason": "IL_CO2"} — пауза + WARNING (интерлок/кризис, не агент)."""
        reason = _hold_reason(value)
        if node["state"] == "PRINTING":
            return {**self._pause(node, None), **_hold_updates(node, reason)}
        if node["state"] == "PAUSED":
            return _hold_updates(node, reason)
        raise ControlError(f"Удержание невозможно в состоянии {node['state']}")

    @control("release_hold")
    def _release_hold(self, node, value):
        """Админ снимает удержание и ALARM (печать не продолжается — нужен resume)."""
        return dict(HOLD_STATE)

    @control("remove_part", physical=True)
    def _remove_part(self, node, value):
        """Оператор снял деталь со стола."""
        if node["state"] in ("PRINTING", "PAUSED"):
            raise ControlError("Идёт печать — деталь снимать нельзя")
        return {"bed_clear": True}

    def real_command(self, store: StateStore, action: str, value: Any = None) -> list[tuple[str, Any]]:
        """Удержание, его снятие и снятие детали — состояние сервера; на Klipper уходит только pause."""
        node = store.get_node(self.node_id)
        if action == "server_hold":
            reason = _hold_reason(value)
            if node["state"] not in ("PRINTING", "PAUSED"):
                raise ControlError(f"Удержание невозможно в состоянии {node['state']}")
            store.update(self.node_id, **_hold_updates(node, reason))
            return [("pause", None)] if node["state"] == "PRINTING" else []
        if action in ("release_hold", "remove_part"):
            store.update(self.node_id, **getattr(self, "_" + action)(node, value))
            return []
        return [(action, value)]

    @control("home")
    def _home(self, node, value):
        self._require_online(node)
        if node["state"] in ("PRINTING", "PAUSED"):
            raise ControlError("HOME недоступен во время печати")
        return {"homed": True, "x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0}

    @control("start_job", "start_print", "print_file")
    def _start_job(self, node, value):
        """value: имя файла из files (или {"file_name": ...})."""
        self._require_online(node)
        if node["state"] not in ("IDLE", "COMPLETED"):
            raise ControlError(f"Принтер занят (состояние {node['state']})")
        file_name, duration, meta = _job_file(self._files, value, 2 * 3600.0, "start_job")
        if meta is not None and not (isinstance(value, dict) and value.get("filament_g") is not None):
            filament = meta["filament_g"]
        else:
            filament = _filament_from_value(value, duration)
        return self._start(file_name, duration, filament)

    @control("upload_file", "upload")
    def _upload_file(self, node, value):
        """value: {"file_name": "part.gcode", "content": "<текст G-code>"}."""
        self._require_online(node)
        if not isinstance(value, dict):
            raise ControlError('upload_file: ожидалось {"file_name": ..., "content": "<G-code>"}')
        name = file_name_from_value(value, PRINTER_EXTENSIONS, "upload_file")
        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ControlError("upload_file: content — непустой текст G-code")
        size = len(content.encode("utf-8"))
        if size > MAX_UPLOAD_BYTES:
            raise ControlError(f"upload_file: файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
        if node["state"] in ("PRINTING", "PAUSED") and node["file_name"] == name:
            raise ControlError(f"upload_file: файл {name!r} сейчас печатается")
        duration, filament = gcode_estimate(content)
        if duration is None:
            duration = max(60.0, 0.05 * content.count("\n"))   # ~20 строк G-code в секунду
        self._files[name] = {"size_bytes": size, "estimated_time_s": round(duration, 1),
                             "filament_g": round(filament, 2) if filament is not None
                             else round(12.0 * duration / 3600.0, 1)}
        self.emit("printer.file_uploaded", file_name=name, size_bytes=size)
        return {"files": sorted(self._files)}

    @control("delete_file")
    def _delete_file(self, node, value):
        """value: имя файла (или {"file_name": ...})."""
        self._require_online(node)
        name = file_name_from_value(value, PRINTER_EXTENSIONS, "delete_file")
        if name not in self._files:
            raise ControlError(f"delete_file: файл {name!r} не найден. Доступно: {sorted(self._files)}")
        if node["state"] in ("PRINTING", "PAUSED") and node["file_name"] == name:
            raise ControlError(f"delete_file: файл {name!r} сейчас печатается")
        del self._files[name]
        return {"files": sorted(self._files)}

    @query("list_files")
    def _list_files(self, node, params):
        return _file_list(self._files)

    @control("set_speed", "set_print_speed")
    def _set_speed(self, node, value):
        """value: скорость печати в процентах 50..200 (или {"pct": ...}) — M220."""
        self._require_online(node)
        pct = value.get("pct") if isinstance(value, dict) else value
        return {"speed_pct": parse_number(pct, 50.0, 200.0, "Скорость печати, %")}

    @staticmethod
    def parse_move(value: Any, travel: tuple[float, float, float], max_relative: float) -> dict[str, Any]:
        """Проверка параметров move. Возвращает {"mode", "x", "y", "z", "feedrate"}."""
        if not isinstance(value, dict):
            raise ControlError('move: ожидалось {"x"?, "y"?, "z"?, "mode": "relative"|"absolute", "feedrate"?}')
        mode = str(value.get("mode", "relative")).strip().lower()
        if mode not in ("relative", "absolute"):
            raise ControlError('move: mode — "relative" или "absolute"')
        move: dict[str, Any] = {"mode": mode,
                                "feedrate": int(parse_number(value.get("feedrate", 3000), 60, 12000,
                                                             "Скорость перемещения, мм/мин"))}
        for axis, limit in zip("xyz", travel):
            if value.get(axis) is None:
                move[axis] = None
            elif mode == "relative":
                move[axis] = parse_number(value[axis], -max_relative, max_relative, f"Смещение {axis.upper()}, мм")
            else:
                move[axis] = parse_number(value[axis], 0.0, limit, f"Координата {axis.upper()}, мм")
        if all(move[a] is None for a in "xyz"):
            raise ControlError("move: укажите хотя бы одну ось (x, y, z)")
        return move

    @control("move", "move_axes", "jog")
    def _move(self, node, value):
        """value: {"x", "y", "z" (необязательные), "mode": "relative" (по умолчанию) | "absolute",
        "feedrate": мм/мин}. Относительное смещение — не более ±50 мм по оси."""
        self._require_online(node)
        move = self.parse_move(value, self.TRAVEL, self.MAX_RELATIVE_MOVE)
        if node["state"] in ("PRINTING", "PAUSED"):
            raise ControlError("move: перемещение недоступно во время печати")
        if not node["homed"]:
            raise ControlError("move: сначала выполните HOME (Must home axis first)")
        u = {}
        for axis, limit in zip("xyz", self.TRAVEL):
            if move[axis] is None:
                continue
            target = move[axis] + node[f"{axis}_mm"] if move["mode"] == "relative" else move[axis]
            if not 0.0 <= target <= limit:
                raise ControlError(f"move: {axis.upper()}={target:g} мм вне области печати 0..{limit:g}")
            u[f"{axis}_mm"] = round(target, 3)
        return u

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
                u.update(homed=True, x_mm=0.0, y_mm=0.0, z_mm=0.0)
            elif cmd == "M220":
                u["speed_pct"] = clamp(params.get("S", 100.0), 1.0, 500.0)
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
            elif cmd in ("M105", "M114", "M115", "M106", "M107", "M84", "M18", "M221"):
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
    "ALARM:5": "Probe fail: probe did not contact the workpiece",
}

# Пробинг Z (probe_z): параметры, которые можно передать в value, их диапазоны и значения
# по умолчанию (как у CNCController.probe_z_zero). Общие для виртуального и реального фрезера.
CNC_PROBE_LIMITS: dict[str, tuple[float, float]] = {
    "distance": (0.1, 30.0),          # мм вниз от текущей точки
    "feed": (1.0, 300.0),             # мм/мин
    "plate_thickness": (0.0, 30.0),   # мм: рабочий Z в точке касания станет равен толщине пластины
    "retract": (0.0, 20.0),           # мм отъезда вверх после касания
    "second_pass_feed": (1.0, 100.0), # мм/мин повторного медленного касания
    "latch_distance": (0.1, 5.0),     # мм отъезда перед повторным касанием
}
CNC_PROBE_DEFAULTS = {"distance": 10.0, "feed": 50.0, "plate_thickness": 0.0, "retract": 2.0}
CNC_AXES = "xyz"


@register_node_type("cnc")
class CNCMill(SandboxNode):
    """
    Lunyee 3018 Pro Max, рабочая область 300×180×80 мм.
    Входы для правил: control_powered (линия 4 щитка), control_coolant_temp_c
    (температура жидкости контура охлаждения → spindle_temp_c).

    Координаты как у GRBL: x_mm/y_mm/z_mm — рабочие (WPos, в них написан G-code),
    mpos_mm — машинные, wco_mm — смещение рабочей системы координат (WPos = MPos − WCO).
    Машинная область модели: X 0..300, Y 0..180, Z −80..10 (Z 0 — после HOME, вниз — минус).
    Поверхность заготовки модели — машинный Z = SURFACE_Z.

    Наладка (только в IDLE): move — перемещение головы (jog GRBL: модальное состояние G90/G91
    не меняется), set_zero — текущая точка становится нулём по выбранным осям (G10 L20 P0),
    probe_z — поиск поверхности пробником (G38.2) и запись Z0; last_probe — результат
    в машинных координатах. Пробник не дотянулся до поверхности — ALARM:5.

    Без питания GRBL сбрасывается: ALARM:3, позиция потеряна (homed=False).
    FeedHold останавливает шпиндель (парковка), Resume — раскручивает снова.
    Качество: spindle_temp_c > 40 °C — DEGRADED, > 45 °C — OVERHEAT (брак, К7).

    Файлы фрезеровки: files — имена G-code файлов в каталоге фрезера,
    запрос list_files — с размером и оценкой времени; start_job — по имени файла.

    Удержание сервером (holds.py): server_hold {"reason"} — FeedHold с остановкой
    шпинделя (на реальном GRBL: «!» + Spindle Stop override 0x9E, при отказе —
    безопасная отмена задания); release_hold — снятие удержания админом.
    """
    title = "ЧПУ-фрезер Lunyee 3018 Pro Max"
    system = "Производство"
    category = "production"

    TRAVEL = (300.0, 180.0, 80.0)
    MACHINE_LIMITS = ((0.0, 300.0), (0.0, 180.0), (-80.0, 10.0))
    SURFACE_Z = -20.0               # машинный Z поверхности заготовки (модель)
    JOG_FEED = (10.0, 5000.0)       # мм/мин; по умолчанию 1000
    SPINDLE_RPM = 10000.0
    SPINDLE_W, STEPPERS_W, IDLE_W = 250.0, 40.0, 25.0

    def __init__(self, node_id: str, auto_jobs: bool = True, job_rate_per_hour: float = 0.25,
                 alarm_rate_per_hour: float = 0.02, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.auto_jobs = auto_jobs
        self.job_rate_per_hour = job_rate_per_hour
        self.alarm_rate_per_hour = alarm_rate_per_hour
        self._files: dict[str, dict[str, Any]] = {
            name: {"size_bytes": int(duration * 30), "estimated_time_s": float(duration), "valid": True}
            for name, duration in CNC_JOBS}

    def initial_state(self) -> dict[str, Any]:
        return {
            "state": "IDLE",               # IDLE | RUNNING | PAUSED | ALARM
            "paused": False,
            "x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0,
            "mpos_mm": [0.0, 0.0, 0.0],
            "wco_mm": [0.0, 0.0, 0.0],
            "last_probe": None,            # {"x_mm", "y_mm", "z_mm" (машинные), "success"}
            "spindle_rpm": 0.0,
            "spindle_target_rpm": 0.0,
            "file_name": None,
            "files": sorted(self._files),  # G-code файлы, доступные для фрезеровки
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
            **HOLD_STATE,
        }

    def _start(self, file_name: str, duration_s: float) -> dict[str, Any]:
        self.emit("cnc.job_started", file_name=file_name, duration_s=duration_s)
        return {"state": "RUNNING", "paused": False, "file_name": file_name, "progress_pct": 0.0,
                "job_elapsed_s": 0.0, "job_duration_s": duration_s, "job_time_left_s": duration_s,
                "spindle_target_rpm": self.SPINDLE_RPM, "part_defect": None}

    def _toolpath(self, t: float) -> tuple[float, float, float]:
        """Правдоподобная траектория фрезеровки платы ~100×70 мм в рабочих координатах (от нуля
        заготовки): обход контуров + редкие холостые ходы."""
        x = 50.0 + 45.0 * math.sin(2 * math.pi * t / 640.0)
        y = 35.0 + 30.0 * math.sin(2 * math.pi * t / 410.0 + 1.1)
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
        u["mpos_mm"] = self._mpos({**node, **u})
        return u

    @staticmethod
    def _mpos(node: dict[str, Any]) -> list[float]:
        """Машинные координаты из рабочих: MPos = WPos + WCO."""
        return [round(float(node[f"{a}_mm"]) + float(w), 3) for a, w in zip(CNC_AXES, node["wco_mm"])]

    def _work(self, node: dict[str, Any], mpos: list[float]) -> dict[str, Any]:
        """Обновления узла для машинной позиции mpos (рабочие координаты пересчитываются)."""
        return {**{f"{a}_mm": round(m - float(w), 3) for a, m, w in zip(CNC_AXES, mpos, node["wco_mm"])},
                "mpos_mm": [round(m, 3) for m in mpos]}

    def _in_travel(self, mpos: list[float]) -> bool:
        return all(lo <= m <= hi for m, (lo, hi) in zip(mpos, self.MACHINE_LIMITS))

    def _require_idle(self, node: dict[str, Any], what: str) -> None:
        self._require_ready(node)
        if node["state"] != "IDLE":
            raise ControlError(f"{what}: станок должен быть в IDLE (сейчас {node['state']})")

    @classmethod
    def parse_move(cls, value: Any) -> dict[str, Any]:
        """move: {"x"?, "y"?, "z"?, "mode": "relative" (по умолчанию) | "absolute", "feedrate"?}.
        Относительно — смещение от текущей точки (не больше хода оси), абсолютно — рабочие
        координаты (от нуля, выставленного set_zero / probe_z). Возвращает {"mode", "x", "y", "z", "feedrate"}."""
        if not isinstance(value, dict):
            raise ControlError('move: ожидалось {"x"?, "y"?, "z"?, "mode": "relative"|"absolute", "feedrate"?}')
        unknown = set(value) - {"x", "y", "z", "mode", "feedrate"}
        if unknown:
            raise ControlError(f"move: неизвестные параметры {sorted(unknown)} (доступно: x, y, z, mode, feedrate)")
        mode = str(value.get("mode", "relative")).strip().lower()
        if mode not in ("relative", "absolute"):
            raise ControlError('move: mode — "relative" или "absolute"')
        lo, hi = cls.JOG_FEED
        move: dict[str, Any] = {"mode": mode,
                                "feedrate": round(parse_number(value.get("feedrate", 1000.0), lo, hi,
                                                               "Скорость перемещения, мм/мин"), 1)}
        for axis, travel in zip(CNC_AXES, cls.TRAVEL):
            raw = value.get(axis)
            move[axis] = None if raw is None else round(parse_number(
                raw, -travel, travel, f"{'Смещение' if mode == 'relative' else 'Координата'} {axis.upper()}, мм"), 3)
        if all(move[a] is None for a in CNC_AXES):
            raise ControlError("move: укажите хотя бы одну ось (x, y, z)")
        return move

    @staticmethod
    def parse_axes(value: Any) -> str:
        """set_zero: оси, которые обнулить: "xyz" (по умолчанию), "xy", "z", ["x", "y"] или {"axes": ...}."""
        if isinstance(value, dict):
            value = value.get("axes")
        if value is None:
            return CNC_AXES
        if isinstance(value, (list, tuple)):
            value = "".join(str(a) for a in value)
        if not isinstance(value, str) or not value.strip():
            raise ControlError('set_zero: оси — строка вроде "xy" или список ["x", "y"]')
        axes = value.strip().lower()
        if set(axes) - set(CNC_AXES) or len(set(axes)) != len(axes):
            raise ControlError(f'set_zero: допустимы оси x, y, z без повторов, получено {value!r}')
        return "".join(a for a in CNC_AXES if a in axes)

    @staticmethod
    def parse_probe(value: Any) -> dict[str, float]:
        """probe_z: необязательный объект с параметрами из CNC_PROBE_LIMITS; только заданные ключи."""
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ControlError(f"probe_z: value — объект с параметрами {', '.join(CNC_PROBE_LIMITS)} "
                               "(все необязательны)")
        unknown = set(value) - set(CNC_PROBE_LIMITS)
        if unknown:
            raise ControlError(f"probe_z: неизвестные параметры {sorted(unknown)}. "
                               f"Доступно: {', '.join(CNC_PROBE_LIMITS)}")
        params: dict[str, float] = {}
        for key, raw in value.items():
            if raw is not None:
                lo, hi = CNC_PROBE_LIMITS[key]
                params[key] = parse_number(raw, lo, hi, f"probe_z: {key}")
        return params

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

    @control("server_hold")
    def _server_hold(self, node, value):
        """Удержание сервером: {"reason": "IL_CONDENSATE"} — FeedHold, шпиндель остановлен."""
        reason = _hold_reason(value)
        if node["state"] == "RUNNING":
            return {**self._feed_hold(node, None), **_hold_updates(node, reason)}
        if node["state"] == "PAUSED":
            return {"spindle_target_rpm": 0.0, **_hold_updates(node, reason)}
        raise ControlError(f"Удержание невозможно в состоянии {node['state']}")

    @control("release_hold")
    def _release_hold(self, node, value):
        """Админ снимает удержание и ALARM (фрезеровка не продолжается — нужен resume)."""
        return dict(HOLD_STATE)

    def real_command(self, store: StateStore, action: str, value: Any = None) -> list[tuple[str, Any]]:
        """server_hold -> драйвер «hold» (FeedHold + остановка шпинделя); release_hold — состояние сервера."""
        node = store.get_node(self.node_id)
        if action == "server_hold":
            reason = _hold_reason(value)
            if node["state"] not in ("RUNNING", "PAUSED"):
                raise ControlError(f"Удержание невозможно в состоянии {node['state']}")
            store.update(self.node_id, **_hold_updates(node, reason))
            return [("hold", None)]
        if action == "release_hold":
            store.update(self.node_id, **HOLD_STATE)
            return []
        return [(action, value)]

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
        # машинный ноль; смещение рабочей СК (WCO) сохраняется, как в EEPROM GRBL
        return {"state": "IDLE", "alarm": False, "alarm_code": None, "homed": True,
                **self._work(node, [0.0, 0.0, 0.0])}

    @control("reset_alarm", "unlock", "$x")
    def _reset_alarm(self, node, value):
        if node["state"] != "ALARM":
            raise ControlError("Фрезер не в состоянии ALARM")
        return {"state": "IDLE", "alarm": False, "alarm_code": None, "paused": False}

    @control("move", "jog")
    def _move(self, node, value):
        """value: {"x"?, "y"?, "z"?, "mode": "relative" (по умолчанию) | "absolute", "feedrate"?: мм/мин}.
        relative — смещение от текущей точки, absolute — рабочие координаты. Только в IDLE.
        Цель за пределами хода станка — отказ (error:15), станок не двигается."""
        move = self.parse_move(value)
        self._require_idle(node, "move")
        mpos = self._mpos(node)
        for i, axis in enumerate(CNC_AXES):
            if move[axis] is not None:
                mpos[i] = mpos[i] + move[axis] if move["mode"] == "relative" \
                    else move[axis] + float(node["wco_mm"][i])
        if not self._in_travel(mpos):
            raise ControlError(f"error:15 Jog target exceeds machine travel: MPos {[round(m, 3) for m in mpos]}, "
                               f"ход X {self.MACHINE_LIMITS[0]}, Y {self.MACHINE_LIMITS[1]}, Z {self.MACHINE_LIMITS[2]}")
        return self._work(node, mpos)

    @control("set_zero", "zero")
    def _set_zero(self, node, value):
        """Текущая точка — ноль рабочей СК по осям value: "xyz" (по умолчанию), "xy", "z", ["x", "y"].
        Как G10 L20 P0 X0 Y0 Z0. Только в IDLE."""
        axes = self.parse_axes(value)
        self._require_idle(node, "set_zero")
        mpos = self._mpos(node)
        wco = [round(mpos[i] if axis in axes else float(w), 3)
               for i, (axis, w) in enumerate(zip(CNC_AXES, node["wco_mm"]))]
        return {**self._work({**node, "wco_mm": wco}, mpos), "wco_mm": wco}

    @control("probe_z")
    def _probe_z(self, node, value):
        """Поиск поверхности пробником (G38.2) и запись Z0: рабочий Z в точке касания = plate_thickness,
        затем отъезд вверх на retract. value — необязательный объект: distance (ход вниз, мм),
        feed, plate_thickness, retract, second_pass_feed, latch_distance. Только в IDLE.
        Касания нет на всём ходе — ALARM:5 (снять: reset_alarm или home)."""
        p = {**CNC_PROBE_DEFAULTS, **self.parse_probe(value)}
        self._require_idle(node, "probe_z")
        mpos = self._mpos(node)
        contact = self.SURFACE_Z + p["plate_thickness"]     # пластина лежит на заготовке
        if mpos[2] < contact - 1e-6:
            raise ControlError("probe_z: пробник уже показывает контакт (Pn:P) — поднимите Z")
        if mpos[2] - p["distance"] > contact:
            mpos[2] -= p["distance"]
            self.emit("cnc.alarm", Severity.WARNING, alarm_code="ALARM:5")
            return {**self._work(node, mpos), "state": "ALARM", "alarm": True, "alarm_code": "ALARM:5",
                    "error_text": GRBL_ALARMS["ALARM:5"],
                    "last_probe": {"x_mm": mpos[0], "y_mm": mpos[1], "z_mm": round(mpos[2], 3), "success": False}}
        wco = [float(w) for w in node["wco_mm"]]
        wco[2] = round(contact - p["plate_thickness"], 3)   # рабочий Z касания = толщина пластины
        touch = list(mpos)
        touch[2] = contact
        mpos[2] = min(contact + p["retract"], self.MACHINE_LIMITS[2][1])
        return {**self._work({**node, "wco_mm": wco}, mpos), "wco_mm": wco, "error_text": None,
                "last_probe": {"x_mm": touch[0], "y_mm": touch[1], "z_mm": round(touch[2], 3), "success": True}}

    @control("start_job")
    def _start_job(self, node, value):
        self._require_ready(node)
        if node["state"] != "IDLE":
            raise ControlError(f"Фрезер занят (состояние {node['state']})")
        file_name, duration, _ = _job_file(self._files, value, 3600.0, "start_job")
        return self._start(file_name, duration)

    @query("list_files")
    def _list_files(self, node, params):
        return _file_list(self._files)

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
                pos = [u.get(f"{a}_mm", node[f"{a}_mm"]) for a in CNC_AXES]
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
                mpos = [p + float(w) for p, w in zip(pos, node["wco_mm"])]
                if not self._in_travel(mpos):
                    self.emit("cnc.alarm", Severity.WARNING, alarm_code="ALARM:2")
                    u.update(state="ALARM", alarm=True, alarm_code="ALARM:2", spindle_target_rpm=0.0)
                    break
                u.update(self._work(node, mpos))
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

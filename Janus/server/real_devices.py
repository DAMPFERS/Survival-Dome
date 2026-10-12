"""
Адаптеры реального оборудования для узлов симулятора купола.

Каждый адаптер связывает одно или несколько реальных устройств с узлом
симулятора (node_id):
    read()              -> обновления параметров узла из реального устройства
                           (None — данных нет, узел не трогаем);
    available()         -> можно ли сейчас переключить узел в режим real;
    execute(action, v)  -> выполнить команду на железе (async; action — основное
                           имя действия узла). Неподдерживаемое действие —
                           RealCommandError с понятным текстом.

Модули оборудования импортируются по отдельности: отсутствие библиотеки
одного устройства не отключает остальные.

Статус: адаптеры принтера и ЧПУ написаны по коду модулей 3d-print/ и frezer/
и ещё не проверены на реальном оборудовании. Адаптер ЧПУ проверен на эмуляторе
GRBL с cnc_controller.py (GRBL 1.1, протокол send-response).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("RealDevices")

SERVER_DIR = Path(__file__).parent


class RealCommandError(Exception):
    """Команда не может быть выполнена на реальном устройстве."""


def _import_optional(module: str, extra_path: Optional[Path] = None):
    if extra_path is not None and str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))
    try:
        return __import__(module, fromlist=["*"])
    except Exception as e:  # ImportError, ошибки инициализации драйверов и т.п.
        logger.warning("Модуль оборудования %s недоступен: %s", module, e)
        return None


class RealDeviceAdapter:
    node_id: str = ""
    title: str = ""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def available(self) -> bool:
        return False

    def read(self) -> Optional[dict[str, Any]]:
        return None

    async def execute(self, action: str, value: Any = None) -> None:
        raise RealCommandError(f"{self.title}: управление реальным устройством не поддерживается")

    def status(self) -> dict[str, Any]:
        return {"title": self.title, "available": self.available()}


# ---------------------------------------------------------------------------
# Инвертор MUST + АКБ (общий монитор Modbus)
# ---------------------------------------------------------------------------

class _InverterSource:
    """Общий InverterMonitor для инвертора и АКБ."""

    def __init__(self, module, poll_interval: float = 2.0) -> None:
        self.monitor = None
        self._module = module
        self._poll_interval = poll_interval

    def start(self) -> None:
        if self.monitor is None and self._module is not None:
            try:
                self.monitor = self._module.InverterMonitor(poll_interval=self._poll_interval)
                self.monitor.start()
                logger.info("InverterMonitor запущен")
            except Exception as e:
                logger.error("InverterMonitor не запущен: %s", e)
                self.monitor = None

    def stop(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None

    def snapshot(self):
        if self.monitor is None:
            return None
        snap = self.monitor.get_snapshot()
        return snap if snap.is_online else None


class InverterAdapter(RealDeviceAdapter):
    node_id = "solar_inverter_01"
    title = "Инвертор MUST PH1800"

    def __init__(self, source: _InverterSource) -> None:
        self.source = source

    def start(self) -> None:
        self.source.start()

    def stop(self) -> None:
        self.source.stop()

    def available(self) -> bool:
        return self.source.snapshot() is not None

    def read(self) -> Optional[dict[str, Any]]:
        snap = self.source.snapshot()
        if snap is None:
            return {"online": False} if self.source.monitor is not None else None
        # Поля *_kw модуля solar на самом деле в ваттах (регистры без множителя)
        updates: dict[str, Any] = {"online": True}
        if snap.generated_power_kw is not None:
            updates["pv_power_w"] = round(float(snap.generated_power_kw), 1)
        if snap.consumed_power_kw is not None:
            updates["load_power_w"] = round(float(snap.consumed_power_kw), 1)
        if snap.battery_soc_percent is not None:
            updates["battery_soc_pct"] = round(float(snap.battery_soc_percent), 1)
        if snap.battery_voltage_v is not None:
            updates["battery_voltage_v"] = round(float(snap.battery_voltage_v), 2)
        return updates


class BatteryAdapter(RealDeviceAdapter):
    node_id = "battery_01"
    title = "АКБ (через инвертор)"

    def __init__(self, source: _InverterSource) -> None:
        self.source = source

    def available(self) -> bool:
        return self.source.snapshot() is not None

    def read(self) -> Optional[dict[str, Any]]:
        snap = self.source.snapshot()
        if snap is None:
            return None
        updates: dict[str, Any] = {}
        if snap.battery_soc_percent is not None:
            updates["soc_pct"] = round(float(snap.battery_soc_percent), 1)
        if snap.battery_voltage_v is not None:
            updates["voltage_v"] = round(float(snap.battery_voltage_v), 2)
        return updates or None


# ---------------------------------------------------------------------------
# Умный щиток (реле Tuya L1..L8) и датчик климата
# ---------------------------------------------------------------------------

class _SmartSource:
    def __init__(self, module, poll_interval: float = 4.0) -> None:
        self.manager = None
        self._module = module
        self._poll_interval = poll_interval

    def start(self) -> None:
        if self.manager is None and self._module is not None:
            m = self._module
            try:
                self.manager = m.SmartDeviceManager(
                    switch_configs=[m.L1, m.L2, m.L3, m.L4, m.L5, m.L6, m.L7, m.L8],
                    sensor_config=m.CO2_SENSOR,
                    poll_interval=self._poll_interval,
                )
                self.manager.start()
                logger.info("SmartDeviceManager запущен")
            except Exception as e:
                logger.error("SmartDeviceManager не запущен: %s", e)
                self.manager = None

    def stop(self) -> None:
        if self.manager is not None:
            self.manager.stop()
            self.manager = None


class SmartPanelAdapter(RealDeviceAdapter):
    """Реле L1..L8 -> линии 1..8 узла smart_panel_01."""
    node_id = "smart_panel_01"
    title = "Умный щиток (реле L1–L8)"
    LINES = 8

    def __init__(self, source: _SmartSource, current_lines) -> None:
        self.source = source
        self._current_lines = current_lines  # callable -> текущий список lines из симулятора

    def start(self) -> None:
        self.source.start()

    def stop(self) -> None:
        self.source.stop()

    @staticmethod
    def _responded(sw: Optional[dict]) -> bool:
        # SmartDeviceManager не хранит признак связи: до первого успешного опроса
        # напряжение реле = 0, у живого реле в сети оно всегда ~230 В
        return bool(sw) and float(sw.get("voltage", 0.0)) > 0.0

    def available(self) -> bool:
        m = self.source.manager
        return m is not None and any(self._responded(sw) for sw in m.get_all_switch_data().values())

    def read(self) -> Optional[dict[str, Any]]:
        if self.source.manager is None:
            return None
        data = self.source.manager.get_all_switch_data()
        lines = self._current_lines()
        for n in range(1, self.LINES + 1):
            sw = data.get(f"L{n}")
            if not self._responded(sw):
                continue  # реле не отвечает — оставляем последние известные данные линии
            on = bool(sw.get("state", False))
            line = lines[n - 1]
            # Срабатывание защиты реле не сообщают: сохраняем TRIPPED/RCD_TRIP, пока линия выключена
            if not on and line["state"] in ("TRIPPED", "RCD_TRIP"):
                state = line["state"]
            else:
                state = "ON" if on else "OFF"
            line.update(state=state, power_w=round(float(sw.get("power", 0.0)), 1),
                        current_a=round(float(sw.get("current", 0.0)), 3),
                        voltage_v=round(float(sw.get("voltage", 0.0)), 1))
        return {
            "lines": lines,
            "total_power_w": round(sum(l["power_w"] for l in lines), 1),
            "total_current_a": round(sum(l["current_a"] for l in lines), 2),
            "lines_on_count": sum(1 for l in lines if l["state"] == "ON"),
        }

    async def execute(self, action: str, value: Any = None) -> None:
        manager = self.source.manager
        if manager is None:
            raise RealCommandError("Реле щитка недоступны")
        line = value.get("line") if isinstance(value, dict) else value
        try:
            n = int(line)
        except (TypeError, ValueError):
            raise RealCommandError(f"Номер линии: ожидалось 1..{self.LINES}, получено {value!r}") from None
        if not 1 <= n <= self.LINES:
            raise RealCommandError(f"Номер линии: ожидалось 1..{self.LINES}, получено {n}")
        label = f"L{n}"
        if action == "line_on":
            state = True
        elif action == "line_off":
            state = False
        elif action == "toggle_line":
            state = not manager.get_switch_state(label)
        else:
            raise RealCommandError(f"Реле щитка не поддерживают действие {action!r} "
                                   "(доступно: line_on, line_off, toggle_line)")
        ok = await asyncio.to_thread(manager.set_switch, label, state)
        if not ok:
            raise RealCommandError(f"Реле {label} не ответило")


class ClimateAdapter(RealDeviceAdapter):
    node_id = "climate_sensor_01"
    title = "Датчик CO2 / климата (Tuya)"

    def __init__(self, source: _SmartSource) -> None:
        self.source = source

    @staticmethod
    def _responded(s: Optional[dict]) -> bool:
        # до первого успешного опроса все показания нулевые
        return bool(s) and (s.get("co2", 0) > 0 or s.get("humidity", 0.0) > 0.0)

    def available(self) -> bool:
        m = self.source.manager
        return m is not None and self._responded(m.get_sensor_data())

    def read(self) -> Optional[dict[str, Any]]:
        if self.source.manager is None:
            return None
        s = self.source.manager.get_sensor_data()
        if not self._responded(s):
            return None
        from dome_simulator.environment import dew_point_c
        t, h = float(s.get("temperature", 0.0)), float(s.get("humidity", 0.0))
        return {"co2_ppm": s.get("co2", 0), "temperature_c": t, "humidity_pct": h,
                "dew_point_c": round(dew_point_c(t, h), 2) if h > 0 else None}


# ---------------------------------------------------------------------------
# 3D-принтер (Klipper / Moonraker)
# ---------------------------------------------------------------------------

KLIPPER_STATE_MAP = {
    "standby": "IDLE",
    "printing": "PRINTING",
    "paused": "PAUSED",
    "complete": "COMPLETED",
    "cancelled": "IDLE",
    "error": "ERROR",
}


class PrinterAdapter(RealDeviceAdapter):
    node_id = "printer_3d_01"
    title = "3D-принтер (Klipper)"

    def __init__(self, host: str, port: Optional[int] = None, poll_interval: float = 1.0) -> None:
        self.host, self.port, self.poll_interval = host, port, poll_interval
        self._module = _import_optional("klipper_printer", SERVER_DIR / "3d-print")
        self.printer = None

    def start(self) -> None:
        if self._module is None or self.printer is not None:
            return
        self.printer = self._module.KlipperPrinter(host=self.host, port=self.port,
                                                   poll_interval=self.poll_interval)
        self.printer.start()
        logger.info("KlipperPrinter %s запущен", self.host)

    def stop(self) -> None:
        if self.printer is not None:
            self.printer.stop()
            self.printer = None

    def available(self) -> bool:
        return self.printer is not None and self.printer.get_status().online

    def read(self) -> Optional[dict[str, Any]]:
        if self.printer is None:
            return None
        s = self.printer.get_status()
        if not s.online:
            return {"online": False}
        state = KLIPPER_STATE_MAP.get(s.print_state, "IDLE")
        error_code = None
        if not s.klippy_connected:
            state, error_code = "ERROR", f"KLIPPY_{s.klippy_state.upper()}"
        elif state == "ERROR":
            error_code = "PRINT_ERROR"
        return {
            "online": True,
            "state": state,
            "paused": s.paused,
            "nozzle_temp_c": s.extruder_temperature,
            "nozzle_target_c": s.extruder_target,
            "bed_temp_c": s.bed_temperature,
            "bed_target_c": s.bed_target,
            "file_name": s.filename or None,
            "progress_pct": round(s.progress * 100.0, 1),
            "error_code": error_code,
            "gcode_error": s.error or (s.klippy_message or None),
            "webhook_state": s.klippy_state.upper(),  # состояние Klipper из объекта webhooks Moonraker
            "real_state": state,
        }

    async def execute(self, action: str, value: Any = None) -> None:
        p = self.printer
        if p is None:
            raise RealCommandError("Принтер не подключен")
        if action == "pause":
            call = (p.pause,)
        elif action == "resume":
            call = (p.resume,)
        elif action == "abort":
            call = (p.cancel_print,)
        elif action == "home":
            call = (p.home,)
        elif action == "send_gcode":
            if not isinstance(value, str) or not value.strip():
                raise RealCommandError("send_gcode: ожидалась непустая строка G-code")
            call = (p.send_gcode, value)
        else:
            raise RealCommandError(f"Реальный принтер не поддерживает {action!r} "
                                   "(доступно: pause, resume, abort, home, send_gcode)")
        ok = await asyncio.to_thread(*call)
        if not ok:
            raise RealCommandError(f"Принтер не выполнил {action}: {p.get_status().error}")


# ---------------------------------------------------------------------------
# ЧПУ-фрезер (GRBL через ESP32 TCP bridge)
# ---------------------------------------------------------------------------

# Состояние GRBL (machine_state без подсостояния: "Hold:0" -> "Hold") -> state узла.
# Ключ (milling, machine_state); milling=None — подходит для любого значения.
# Имена состояний узла пока не сверены с симулятором — при необходимости поправить здесь.
CNC_STATE_MAP: dict[tuple[Optional[bool], str], str] = {
    (None, "Alarm"): "ALARM",
    (None, "Door"): "ERROR",
    (None, "Home"): "HOMING",
    (True, "Run"): "RUNNING",
    (True, "Hold"): "PAUSED",
    (True, "Idle"): "RUNNING",      # между строками задания / ещё не началось движение
    (False, "Idle"): "IDLE",
    (False, "Run"): "BUSY",         # пробинг, отъезд, ручная команда
    (False, "Jog"): "BUSY",
    (False, "Hold"): "PAUSED",
    (None, "Check"): "IDLE",
    (None, "Sleep"): "IDLE",
}

# Параметры probe_z, которые можно передать в value, и допустимые диапазоны
CNC_PROBE_LIMITS: dict[str, tuple[float, float]] = {
    "distance": (0.1, 30.0),          # мм вниз от текущей точки
    "feed": (1.0, 300.0),             # мм/мин
    "plate_thickness": (0.0, 30.0),   # мм
    "retract": (0.0, 20.0),           # мм
    "second_pass_feed": (1.0, 100.0), # мм/мин
    "latch_distance": (0.1, 5.0),     # мм
}

# $-команды, разрешённые в send_gcode (только чтение). Homing и снятие Alarm —
# отдельными действиями home / reset_alarm; запись настроек ($N=..., $RST) запрещена.
CNC_SAFE_DOLLAR = {"$G", "$#", "$I"}


class CNCAdapter(RealDeviceAdapter):
    """
    Действия (имена основных действий узла):
        start_job    value = имя файла (или {"file_name": ...}) из каталога gcode_dir —
                     произвольные пути с сервера запрещены;
        feed_hold    пауза задания (идемпотентно: без задания ничего не делает);
        resume       продолжение задания (идемпотентно);
        abort        прервать задание (feed hold -> остановка -> soft reset, позиция сохраняется);
        home         $H;
        reset_alarm  $X — снять Alarm без homing (позиция может быть неверной);
        send_gcode   одна строка G-code (из $-команд — только чтение: $G, $#, $I);
        probe_z      поиск нуля Z пробником; value — необязательный dict с параметрами
                     из CNC_PROBE_LIMITS.

    home / reset_alarm / probe_z / send_gcode выполняются в worker-потоке контроллера
    дольше таймаута команды сервера. Поэтому execute() ждёт результат COMMAND_GRACE_S:
    мгновенные ошибки (error:N, Alarm, занятость) уходят клиенту, а если команда ещё идёт —
    она считается принятой, итог виден в телеметрии (state / error_text).

    Подключение к ESP32 — в фоновом потоке, чтобы не блокировать цикл синхронизации:
    без связи CNCController.start() ждёт до connect_timeout + 2 с. После первого
    подключения обрывы контроллер обрабатывает сам.
    """
    node_id = "cnc_01"
    title = "ЧПУ-фрезер 3018 Pro (GRBL)"
    RETRY_S = 30.0
    COMMAND_GRACE_S = 2.0
    GCODE_GRACE_S = 5.0

    def __init__(self, host: str, port: int = 8881, gcode_dir: Optional[Path] = None) -> None:
        self.host, self.port = host, port
        self.gcode_dir = (gcode_dir or SERVER_DIR / "frezer" / "gcode").resolve()
        self._module = _import_optional("cnc_controller", SERVER_DIR / "frezer")
        self.cnc = None
        self._lock = threading.Lock()
        self._connecting = False
        self._stopped = False
        self._last_attempt = float("-inf")
        self._pending = None            # Future последней фоновой команды
        self._pending_action: Optional[str] = None
        self._acked_error: Optional[str] = None   # ошибка контроллера, «погашенная» успешной командой

    # ---------------- жизненный цикл ----------------

    def start(self) -> None:
        with self._lock:
            self._stopped = False
        self.ensure_started()

    def ensure_started(self) -> None:
        """Запускает попытку подключения в фоне не чаще раза в RETRY_S. Не блокирует."""
        with self._lock:
            if self._module is None or self._stopped or self._connecting:
                return
            if self.cnc is not None and self.cnc.is_running():
                return
            if time.monotonic() - self._last_attempt < self.RETRY_S:
                return
            self._last_attempt = time.monotonic()
            self._connecting = True
        threading.Thread(target=self._connect, name="CNC-connect", daemon=True).start()

    def _connect(self) -> None:
        try:
            old = self.cnc
            if old is not None:          # потоки контроллера умерли — пересоздаём
                try:
                    old.stop()
                except Exception:
                    pass
            cnc = self._module.CNCController(ip=self.host, port=self.port)
            try:
                cnc.start()
            except Exception as e:
                logger.warning("ЧПУ %s:%d недоступен: %s", self.host, self.port, e)
                with self._lock:
                    if self.cnc is old:
                        self.cnc = None
                return
            with self._lock:
                stopped = self._stopped
                if not stopped:
                    self.cnc = cnc
            if stopped:                  # stop() вызвали, пока подключались
                cnc.stop()
                return
            logger.info("CNCController %s:%d запущен", self.host, self.port)
        finally:
            with self._lock:
                self._connecting = False

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            cnc, self.cnc = self.cnc, None
        if cnc is not None:
            cnc.stop()   # при идущей фрезеровке — безопасная остановка (может занять до ~40 с)

    # ---------------- состояние ----------------

    @staticmethod
    def _grbl_responds(s) -> bool:
        # online у контроллера = есть TCP до ESP32; GRBL ответил, если состояние известно
        return s.online and s.machine_state != "Unknown"

    def available(self) -> bool:
        cnc = self.cnc
        return cnc is not None and self._grbl_responds(cnc.get_state())

    def _busy(self) -> bool:
        return self._pending is not None and not self._pending.done()

    def _node_state(self, s) -> tuple[str, Optional[str]]:
        base = s.machine_state.split(":", 1)[0]
        if s.milling and s.paused:
            state = "PAUSED"
        else:
            state = (CNC_STATE_MAP.get((s.milling, base))
                     or CNC_STATE_MAP.get((None, base))
                     or ("RUNNING" if s.milling else "IDLE"))
        if state == "IDLE" and self._busy():
            state = "HOMING" if self._pending_action == "home" else "BUSY"
        error_code = None
        if base == "Alarm":
            error_code = f"ALARM_{s.alarm}" if s.alarm else "ALARM"
        elif base == "Door":
            error_code = "SAFETY_DOOR"
        return state, error_code

    def read(self) -> Optional[dict[str, Any]]:
        self.ensure_started()
        cnc = self.cnc
        if cnc is None:
            return None
        s = cnc.get_state()
        if not self._grbl_responds(s):
            return {"online": False, "error_text": s.error}
        state, error_code = self._node_state(s)
        error = s.error if s.error != self._acked_error else None
        return {
            "online": True,
            "state": state,
            "paused": s.paused,
            # WPos неизвестны, пока GRBL не прислал WCO (1–3 с после подключения)
            "x_mm": round(s.x, 3) if s.x is not None else None,
            "y_mm": round(s.y, 3) if s.y is not None else None,
            "z_mm": round(s.z, 3) if s.z is not None else None,
            "spindle_rpm": s.spindle_rpm,   # из отчёта FS; spindle_on обновляется только по $G
            "file_name": Path(s.current_file).name if s.current_file else None,
            "progress_pct": round(s.progress, 1),
            "error_code": error_code,
            "error_text": error,
            "real_state": s.machine_state,  # сырое состояние GRBL (Idle, Run, Hold:0, Alarm, ...)
        }

    # ---------------- команды ----------------

    def _job_path(self, value: Any) -> Path:
        name = value.get("file_name") if isinstance(value, dict) else value
        if not isinstance(name, str) or not name.strip():
            raise RealCommandError("start_job: ожидалось имя файла из каталога G-code")
        path = (self.gcode_dir / name).resolve()
        if path.parent != self.gcode_dir:
            raise RealCommandError("start_job: допускается только имя файла из каталога G-code")
        if not path.is_file():
            raise RealCommandError(f"start_job: файл {name!r} не найден в {self.gcode_dir}")
        return path

    @staticmethod
    def _probe_params(value: Any) -> dict[str, float]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise RealCommandError("probe_z: value — объект с параметрами "
                                   f"{', '.join(CNC_PROBE_LIMITS)} (все необязательны)")
        unknown = set(value) - set(CNC_PROBE_LIMITS)
        if unknown:
            raise RealCommandError(f"probe_z: неизвестные параметры {sorted(unknown)}. "
                                   f"Доступно: {', '.join(CNC_PROBE_LIMITS)}")
        params: dict[str, float] = {}
        for key, raw in value.items():
            if raw is None:
                continue
            lo, hi = CNC_PROBE_LIMITS[key]
            try:
                v = float(raw)
            except (TypeError, ValueError):
                raise RealCommandError(f"probe_z: {key} — ожидалось число, получено {raw!r}") from None
            if not lo <= v <= hi:
                raise RealCommandError(f"probe_z: {key} должен быть в диапазоне {lo:g}..{hi:g}")
            params[key] = v
        return params

    @staticmethod
    def _gcode_line(value: Any) -> str:
        line = value.get("gcode") if isinstance(value, dict) else value
        if not isinstance(line, str) or not line.strip():
            raise RealCommandError("send_gcode: ожидалась непустая строка G-code")
        line = line.strip()
        if "\n" in line or "\r" in line:
            raise RealCommandError("send_gcode: допускается ровно одна строка")
        if line.startswith("$") and line.upper() not in CNC_SAFE_DOLLAR:
            raise RealCommandError(f"send_gcode: из $-команд разрешены только {sorted(CNC_SAFE_DOLLAR)}; "
                                   "для $H / $X — действия home / reset_alarm")
        return line

    def _on_done(self, fut) -> None:
        """Итог фоновой команды (вызывается в потоке контроллера)."""
        if fut.cancelled():
            return
        cnc = self.cnc
        if fut.exception() is None:
            # успешная команда «гасит» старую ошибку в телеметрии
            self._acked_error = cnc.get_state().error if cnc is not None else None
        else:
            self._acked_error = None

    def _submit(self, action: str, factory, grace: float) -> None:
        """Ставит команду контроллера в очередь и ждёт до grace секунд (в потоке)."""
        if self._busy():
            raise RealCommandError(f"Фрезер выполняет {self._pending_action} — дождитесь завершения")
        fut = factory()
        self._pending, self._pending_action = fut, action
        fut.add_done_callback(self._on_done)
        try:
            fut.result(timeout=grace)
        except concurrent.futures.TimeoutError:
            logger.info("Фрезер: %s принята, выполняется", action)

    def _execute_sync(self, c, action: str, value: Any) -> None:
        if action == "feed_hold":
            c.pause()
        elif action == "resume":
            c.resume()
        elif action == "abort":
            if not c.get_state().milling:
                raise RealCommandError("Нет выполняемого задания")
            c.abort_milling()
        elif action == "start_job":
            path = self._job_path(value)
            if self._busy():
                raise RealCommandError(f"Фрезер выполняет {self._pending_action} — дождитесь завершения")
            # Alarm проверяет сам контроллер; Run/Jog (хвост предыдущего движения) не мешают —
            # GRBL поставит строки задания в очередь после него
            st = c.get_state()
            if not st.milling and st.machine_state.split(":", 1)[0] in ("Home", "Hold", "Door", "Sleep"):
                raise RealCommandError(f"Станок не готов к заданию: {st.machine_state}")
            c.start_milling(path)
        elif action == "home":
            self._submit(action, c.home, self.COMMAND_GRACE_S)
            return
        elif action == "reset_alarm":
            self._submit(action, c.unlock, self.COMMAND_GRACE_S)
            return
        elif action == "probe_z":
            params = self._probe_params(value)
            self._submit(action, lambda: c.probe_z_zero(**params), self.COMMAND_GRACE_S)
            return
        elif action == "send_gcode":
            line = self._gcode_line(value)
            self._submit(action, lambda: c.send_command(line), self.GCODE_GRACE_S)
            return
        else:
            raise RealCommandError(f"Реальный фрезер не поддерживает {action!r} (доступно: feed_hold, "
                                   "resume, abort, home, reset_alarm, start_job, send_gcode, probe_z)")
        self._acked_error = c.get_state().error

    async def execute(self, action: str, value: Any = None) -> None:
        c = self.cnc
        if c is None or not self._grbl_responds(c.get_state()):
            raise RealCommandError("Фрезер не подключен")
        try:
            await asyncio.to_thread(self._execute_sync, c, action, value)
        except RealCommandError:
            raise
        except Exception as e:  # CNCBusyError, CNCAlarmError, CNCGrblError, CNCProbeError, ...
            raise RealCommandError(f"Фрезер: {e}") from None

# ---------------------------------------------------------------------------
# Сборка
# ---------------------------------------------------------------------------

def _enabled_nodes() -> Optional[set[str]]:
    """DOME_REAL_DEVICES: "all" (по умолчанию), "none" или список node_id через запятую."""
    raw = os.getenv("DOME_REAL_DEVICES", "all").strip().lower()
    if raw in ("", "all", "*"):
        return None
    if raw == "none":
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def build_adapters(current_lines) -> dict[str, RealDeviceAdapter]:
    """
    current_lines — callable, возвращающий список lines щитка из симулятора.
    DOME_REAL_DEVICES ограничивает набор реальных устройств (none — только симулятор).
    Хосты принтера и фрезера — PRINTER_HOST / CNC_HOST; пустое значение отключает устройство.
    """
    enabled = _enabled_nodes()
    if enabled is not None and not enabled:
        logger.info("Реальные устройства отключены (DOME_REAL_DEVICES=none)")
        return {}
    wanted = (lambda node_id: True) if enabled is None else (lambda node_id: node_id in enabled)
    adapters: list[RealDeviceAdapter] = []

    solar = _import_optional("solar.solar_inverter", SERVER_DIR)         if wanted("solar_inverter_01") or wanted("battery_01") else None
    if solar is not None:
        inverter = _InverterSource(solar)
        adapters += [InverterAdapter(inverter), BatteryAdapter(inverter)]

    smart = _import_optional("smart_rele.smart_devices", SERVER_DIR)         if wanted("smart_panel_01") or wanted("climate_sensor_01") else None
    if smart is not None:
        source = _SmartSource(smart)
        adapters += [SmartPanelAdapter(source, current_lines), ClimateAdapter(source)]

    printer_host = os.getenv("PRINTER_HOST", "192.168.8.69").strip()
    if printer_host and wanted("printer_3d_01"):
        port = os.getenv("PRINTER_PORT", "").strip()
        adapters.append(PrinterAdapter(printer_host, int(port) if port else None))

    cnc_host = os.getenv("CNC_HOST", "192.168.8.71").strip()
    if cnc_host and wanted("cnc_01"):
        gcode_dir = os.getenv("CNC_GCODE_DIR", "").strip()
        adapters.append(CNCAdapter(cnc_host, int(os.getenv("CNC_PORT", "8881")),
                                   Path(gcode_dir) if gcode_dir else None))

    return {a.node_id: a for a in adapters if wanted(a.node_id)}

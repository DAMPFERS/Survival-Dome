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

Запросы данных (query) — async query(name, params), например list_files.

Статус: адаптеры принтера и ЧПУ написаны по коду модулей 3d-print/ и frezer/
и ещё не проверены на реальном оборудовании. Адаптер ЧПУ проверен на эмуляторе
GRBL с cnc_controller.py (GRBL 1.1, протокол send-response). Адаптеры теплиц
МС-ТЮК (TYC/) проверены только на эмуляции HTTP API стендов.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import sys
import tempfile
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

    async def query(self, name: str, params: Any = None) -> Any:
        """Запрос данных у реального устройства (например, list_files)."""
        raise RealCommandError(f"{self.title}: запрос {name!r} реальным устройством не поддерживается")

    def status(self) -> dict[str, Any]:
        return {"title": self.title, "available": self.available()}


def _num(value: Any, lo: float, hi: float, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RealCommandError(f"{what}: ожидалось число, получено {value!r}") from None
    if not lo <= number <= hi:
        raise RealCommandError(f"{what}: значение {number:g} вне диапазона [{lo:g}, {hi:g}]")
    return number


def _bool(value: Any, what: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "on", "yes", "0", "false", "off", "no"):
        return value.strip().lower() in ("1", "true", "on", "yes")
    raise RealCommandError(f"{what}: ожидалось true/false (1/0), получено {value!r}")


def _sim_check(fn, *args):
    """Проверка аргументов команды теми же функциями, что и в симуляторе:
    ControlError -> RealCommandError (сообщения для агента одинаковы в обоих режимах)."""
    from dome_simulator import ControlError
    try:
        return fn(*args)
    except ControlError as e:
        raise RealCommandError(str(e)) from None


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
    """
    Действия: pause, resume, abort, home, send_gcode, а также
        start_job    value = имя файла из памяти принтера (или {"file_name": ...});
        upload_file  value = {"file_name": ..., "content": "<текст G-code>"} —
                     сервер пишет content во временный файл и загружает его в Moonraker;
        delete_file  value = имя файла;
        set_speed    value = скорость печати, % (50..200) — M220;
        move         value = {"x"?, "y"?, "z"?, "mode": "relative"|"absolute", "feedrate"?}.
    Запрос list_files — файлы в gcodes/ Moonraker. Список имён (files) в телеметрии
    обновляется раз в FILES_REFRESH_S и сразу после upload/delete.
    """
    node_id = "printer_3d_01"
    title = "3D-принтер (Klipper)"
    FILES_REFRESH_S = 10.0

    def __init__(self, host: str, port: Optional[int] = None, poll_interval: float = 1.0) -> None:
        self.host, self.port, self.poll_interval = host, port, poll_interval
        self._module = _import_optional("klipper_printer", SERVER_DIR / "3d-print")
        self.printer = None
        self._files: list[str] = []
        self._files_at = float("-inf")

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
        if time.monotonic() - self._files_at >= self.FILES_REFRESH_S:
            self._refresh_files()
        return {
            "online": True,
            "state": state,
            "paused": s.paused,
            "nozzle_temp_c": s.extruder_temperature,
            "nozzle_target_c": s.extruder_target,
            "bed_temp_c": s.bed_temperature,
            "bed_target_c": s.bed_target,
            "file_name": s.filename or None,
            "files": list(self._files),
            "progress_pct": round(s.progress * 100.0, 1),
            "speed_pct": round(s.speed_factor * 100.0, 1),
            "x_mm": s.position_x,
            "y_mm": s.position_y,
            "z_mm": s.position_z,
            "error_code": error_code,
            "gcode_error": s.error or (s.klippy_message or None),
            "webhook_state": s.klippy_state.upper(),  # состояние Klipper из объекта webhooks Moonraker
            "real_state": state,
        }

    def _list_files(self) -> list[dict[str, Any]]:
        files = self.printer.list_files() if self.printer is not None else None
        if files is None:
            raise RealCommandError(f"Список файлов принтера недоступен: {self.printer.get_status().error}"
                                   if self.printer is not None else "Принтер не подключен")
        return [{"name": f.get("path"), "size_bytes": f.get("size"), "modified": f.get("modified")}
                for f in files if isinstance(f, dict) and f.get("path")]

    def _refresh_files(self) -> None:
        self._files_at = time.monotonic()
        try:
            self._files = sorted(f["name"] for f in self._list_files())
        except RealCommandError as e:
            logger.debug("Принтер: %s", e)

    @staticmethod
    def _file_name(value: Any, what: str) -> str:
        from dome_simulator.nodes.production import PRINTER_EXTENSIONS, file_name_from_value
        return _sim_check(file_name_from_value, value, PRINTER_EXTENSIONS, what)

    def _upload(self, name: str, content: str) -> bool:
        fd, tmp = tempfile.mkstemp(suffix=".gcode", prefix="dome_upload_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            return self.printer.upload_file(tmp, name)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _command(self, action: str, value: Any) -> tuple:
        """(функция драйвера, аргументы...) для действия узла; проверка аргументов."""
        from dome_simulator.nodes.production import MAX_UPLOAD_BYTES, Printer3D
        p = self.printer
        if action == "pause":
            return (p.pause,)
        if action == "resume":
            return (p.resume,)
        if action == "abort":
            return (p.cancel_print,)
        if action == "home":
            return (p.home,)
        if action == "send_gcode":
            if not isinstance(value, str) or not value.strip():
                raise RealCommandError("send_gcode: ожидалась непустая строка G-code")
            return (p.send_gcode, value)
        if action == "start_job":
            name = self._file_name(value, "start_job")
            if self._files and name not in self._files:
                raise RealCommandError(f"start_job: файл {name!r} не найден. Доступно: {self._files}")
            return (p.start_print, name)
        if action == "upload_file":
            if not isinstance(value, dict):
                raise RealCommandError('upload_file: ожидалось {"file_name": ..., "content": "<G-code>"}')
            name = self._file_name(value, "upload_file")
            content = value.get("content")
            if not isinstance(content, str) or not content.strip():
                raise RealCommandError("upload_file: content — непустой текст G-code")
            if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
                raise RealCommandError(f"upload_file: файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
            return (self._upload, name, content)
        if action == "delete_file":
            return (p.delete_file, self._file_name(value, "delete_file"))
        if action == "set_speed":
            pct = value.get("pct") if isinstance(value, dict) else value
            return (p.set_print_speed, _num(pct, 50.0, 200.0, "Скорость печати, %") / 100.0)
        if action == "move":
            move = _sim_check(Printer3D.parse_move, value, Printer3D.TRAVEL, Printer3D.MAX_RELATIVE_MOVE)
            if self.printer.get_status().print_state in ("printing", "paused"):
                raise RealCommandError("move: перемещение недоступно во время печати")
            fn = p.move_relative if move["mode"] == "relative" else p.move_absolute
            return (fn, move["x"], move["y"], move["z"], move["feedrate"])
        raise RealCommandError(f"Реальный принтер не поддерживает {action!r} (доступно: pause, resume, "
                               "abort, home, send_gcode, start_job, upload_file, delete_file, set_speed, move)")

    async def execute(self, action: str, value: Any = None) -> None:
        p = self.printer
        if p is None:
            raise RealCommandError("Принтер не подключен")
        call = self._command(action, value)
        ok = await asyncio.to_thread(*call)
        if not ok:
            raise RealCommandError(f"Принтер не выполнил {action}: {p.get_status().error}")
        if action in ("upload_file", "delete_file"):
            await asyncio.to_thread(self._refresh_files)

    async def query(self, name: str, params: Any = None) -> Any:
        if name != "list_files":
            raise RealCommandError(f"Реальный принтер не поддерживает запрос {name!r} (доступно: list_files)")
        if self.printer is None:
            raise RealCommandError("Принтер не подключен")
        return await asyncio.to_thread(self._list_files)


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

# $-команды, разрешённые в send_gcode (только чтение). Homing и снятие Alarm —
# отдельными действиями home / reset_alarm; запись настроек ($N=..., $RST) запрещена.
CNC_SAFE_DOLLAR = {"$G", "$#", "$I"}


class CNCAdapter(RealDeviceAdapter):
    """
    Действия (имена основных действий узла):
        start_job    value = имя файла (или {"file_name": ...}) из каталога gcode_dir —
                     произвольные пути с сервера запрещены;
        feed_hold    пауза задания (идемпотентно: без задания ничего не делает);
        hold         удержание сервером (holds.py): feed hold, дождаться Hold:0, остановить
                     шпиндель (Spindle Stop override 0x9E). Если шпиндель не остановился
                     за HOLD_SPINDLE_WAIT_S — безопасная отмена задания (feed hold + reset);
        resume       продолжение задания (идемпотентно);
        abort        прервать задание (feed hold -> остановка -> soft reset, позиция сохраняется);
        home         $H;
        reset_alarm  $X — снять Alarm без homing (позиция может быть неверной);
        send_gcode   одна строка G-code (из $-команд — только чтение: $G, $#, $I);
        probe_z      поиск нуля Z пробником (G38.2 + G10 L20); value — необязательный dict
                     с параметрами из CNC_PROBE_LIMITS (nodes/production.py);
        move         jog GRBL ($J=G91/G90 G21 ... F...): не меняет модальное состояние
                     (G90/G91 файла), цель за soft limits GRBL отклоняет (error:15);
        set_zero     текущая точка — ноль активной рабочей СК по осям (G10 L20 P0 X0 Y0 Z0).
    move / set_zero / probe_z — только в Idle, без задания. Параметры проверяют те же функции,
    что в симуляторе (CNCMill.parse_move / parse_axes / parse_probe).
    Запрос list_files (params: {"validate": bool}) — G-code файлы каталога gcode_dir
    (CNCController.list_gcode_files); имена файлов (files) есть и в телеметрии.

    home / reset_alarm / probe_z / move / set_zero / send_gcode выполняются в worker-потоке контроллера
    дольше таймаута команды сервера. Поэтому execute() ждёт результат COMMAND_GRACE_S:
    мгновенные ошибки (error:N, Alarm, занятость) уходят клиенту, а если команда ещё идёт —
    она считается принятой, итог виден в телеметрии (state / error_text).

    Подключение к ESP32 — в фоновом потоке, чтобы не блокировать цикл синхронизации:
    без связи CNCController.start() ждёт до connect_timeout + 2 с. После первого
    подключения обрывы контроллер обрабатывает сам.
    """
    node_id = "cnc_01"
    title = "ЧПУ-фрезер Lunyee 3018 Pro Max (GRBL)"
    RETRY_S = 30.0
    COMMAND_GRACE_S = 2.0
    GCODE_GRACE_S = 5.0
    FILES_REFRESH_S = 5.0
    HOLD_WAIT_S = 5.0            # торможение до Hold:0
    HOLD_SPINDLE_WAIT_S = 3.0    # остановка шпинделя после 0x9E

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
        self._files: list[str] = []
        self._files_at = float("-inf")

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
            cnc = self._module.CNCController(ip=self.host, port=self.port, gcode_dir=self.gcode_dir)
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
        if time.monotonic() - self._files_at >= self.FILES_REFRESH_S:
            self._files_at = time.monotonic()
            try:
                self._files = sorted(f.name for f in cnc.list_gcode_files(self.gcode_dir))
            except OSError as e:
                logger.debug("Фрезер: каталог G-code недоступен: %s", e)
                self._files = []
        return {
            "online": True,
            "state": state,
            "paused": s.paused,
            # WPos неизвестны, пока GRBL не прислал WCO (1–3 с после подключения)
            "x_mm": round(s.x, 3) if s.x is not None else None,
            "y_mm": round(s.y, 3) if s.y is not None else None,
            "z_mm": round(s.z, 3) if s.z is not None else None,
            "mpos_mm": [round(v, 3) for v in s.mpos] if s.mpos is not None else None,
            "wco_mm": [round(v, 3) for v in s.wco] if s.wco is not None else None,
            "last_probe": None if s.last_probe is None else {
                "x_mm": round(s.last_probe.x, 3), "y_mm": round(s.last_probe.y, 3),
                "z_mm": round(s.last_probe.z, 3), "success": s.last_probe.success},
            "spindle_rpm": s.spindle_rpm,   # из отчёта FS; spindle_on обновляется только по $G
            "file_name": Path(s.current_file).name if s.current_file else None,
            "files": list(self._files),
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
    def _jog_line(move: dict[str, Any]) -> str:
        """Jog GRBL: G91 — смещение, G90 — рабочие координаты; модальное состояние не меняется."""
        axes = " ".join(f"{a.upper()}{move[a]:.3f}" for a in "xyz" if move[a] is not None)
        return f"$J={'G91' if move['mode'] == 'relative' else 'G90'} G21 {axes} F{move['feedrate']:g}"

    @staticmethod
    def _require_setup_state(c, action: str, allowed: tuple[str, ...] = ("Idle",)) -> None:
        """Наладка (move / set_zero / probe_z) — только без задания и в состоянии allowed."""
        st = c.get_state()
        if st.milling:
            raise RealCommandError(f"{action}: идёт задание фрезеровки — дождитесь завершения")
        if st.machine_state.split(":", 1)[0] not in allowed:
            raise RealCommandError(f"{action}: станок должен быть в Idle (сейчас {st.machine_state})")

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

    @staticmethod
    def _wait(c, pred, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                c.refresh_status(timeout=0.5)
            except Exception:  # noqa: BLE001 — нет свежего отчёта, ждём дальше
                pass
            if pred(c.get_state()):
                return True
            time.sleep(0.2)
        return False

    def _hold(self, c) -> None:
        """Пауза со стопом шпинделя; если GRBL не умеет — отмена задания (позиция сохраняется)."""
        if not c.get_state().milling:
            raise RealCommandError("Нет выполняемого задания")
        c.pause()
        if not self._wait(c, lambda s: s.machine_state == "Hold:0", self.HOLD_WAIT_S):
            logger.warning("Фрезер: не дошёл до Hold:0 за %.0f с", self.HOLD_WAIT_S)
        c.spindle_stop()

        def stopped(s) -> bool:
            if s.accessories is not None:
                return not any(ch in s.accessories for ch in "SC")
            return s.spindle_rpm <= 0.0

        if not self._wait(c, stopped, self.HOLD_SPINDLE_WAIT_S):
            logger.warning("Фрезер: шпиндель не остановился на паузе — задание отменяется")
            c.abort_milling()

    def _execute_sync(self, c, action: str, value: Any) -> None:
        if action == "feed_hold":
            c.pause()
        elif action == "hold":
            self._hold(c)
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
            from dome_simulator.nodes.production import CNCMill
            params = _sim_check(CNCMill.parse_probe, value)
            self._require_setup_state(c, action)
            self._submit(action, lambda: c.probe_z_zero(**params), self.COMMAND_GRACE_S)
            return
        elif action == "move":
            from dome_simulator.nodes.production import CNCMill
            line = self._jog_line(_sim_check(CNCMill.parse_move, value))
            self._require_setup_state(c, action, ("Idle", "Jog"))
            self._submit(action, lambda: c.send_command(line), self.COMMAND_GRACE_S)
            return
        elif action == "set_zero":
            from dome_simulator.nodes.production import CNCMill
            axes = _sim_check(CNCMill.parse_axes, value)
            self._require_setup_state(c, action)
            line = "G10 L20 P0 " + " ".join(f"{a.upper()}0" for a in axes)
            self._submit(action, lambda: c.send_command(line), self.COMMAND_GRACE_S)
            return
        elif action == "send_gcode":
            line = self._gcode_line(value)
            self._submit(action, lambda: c.send_command(line), self.GCODE_GRACE_S)
            return
        else:
            raise RealCommandError(f"Реальный фрезер не поддерживает {action!r} (доступно: feed_hold, hold, "
                                   "resume, abort, home, reset_alarm, start_job, send_gcode, probe_z, "
                                   "move, set_zero)")
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

    def _list_files(self, cnc, validate: bool) -> list[dict[str, Any]]:
        try:
            files = cnc.list_gcode_files(self.gcode_dir, validate=validate)
        except OSError as e:
            raise RealCommandError(f"Каталог G-code фрезера недоступен: {e}") from None
        self._files, self._files_at = sorted(f.name for f in files), time.monotonic()
        return [{"name": f.name, "size_bytes": f.size, "modified": f.mtime, "valid": f.valid, "error": f.error}
                for f in files]

    async def query(self, name: str, params: Any = None) -> Any:
        if name != "list_files":
            raise RealCommandError(f"Реальный фрезер не поддерживает запрос {name!r} (доступно: list_files)")
        c = self.cnc
        if c is None:
            raise RealCommandError("Фрезер не подключен")
        validate = _bool(params.get("validate", False), "validate") if isinstance(params, dict) else False
        return await asyncio.to_thread(self._list_files, c, validate)

# ---------------------------------------------------------------------------
# МС-ТЮК: теплицы ФабЛаб и Химлаб (HTTP API стендов, клиенты TYC/)
# ---------------------------------------------------------------------------

class _GreenhouseAdapter(RealDeviceAdapter):
    """Общая часть адаптеров теплиц: клиент TYC опрашивает стенд в своём потоке,
    read() собирает телеметрию узла из кэша клиента, команды — HTTP POST в потоке.
    Невалидные показания (valid=false) приходят в телеметрию как null."""
    module_name = ""
    client_class = ""
    FANS = 2
    DOORS = 2

    def __init__(self, base_url: str, poll_interval: float = 3.0, timeout: float = 5.0) -> None:
        self.base_url, self.poll_interval, self.timeout = base_url, poll_interval, timeout
        self._module = _import_optional(self.module_name, SERVER_DIR)
        self.client = None

    def start(self) -> None:
        if self._module is None or self.client is not None:
            return
        self.client = getattr(self._module, self.client_class)(
            base_url=self.base_url, poll_interval=self.poll_interval, timeout=self.timeout)
        self.client.start()
        logger.info("%s: опрос %s запущен", self.title, self.base_url)

    def stop(self) -> None:
        if self.client is not None:
            self.client.stop()
            self.client = None

    def available(self) -> bool:
        return self.client is not None and self.client.is_connected()

    def _v(self, param: str, idx: Optional[int] = None) -> Any:
        data = self.client.get(param, idx)
        return data.value if data is not None and data.valid else None

    def _list(self, param: str, count: int, conv=lambda v: v) -> list[Any]:
        return [None if (v := self._v(param, i)) is None else conv(v) for i in range(count)]

    @staticmethod
    def _adc(value: Any) -> tuple[Optional[int], Optional[float]]:
        """ADC и напряжение аналогового датчика: {"adc", "voltage_v" | "voltage_mv"} или число ADC."""
        if isinstance(value, dict):
            adc, volts = value.get("adc"), value.get("voltage_v")
            if volts is None and value.get("voltage_mv") is not None:
                volts = float(value["voltage_mv"]) / 1000.0
        else:
            adc, volts = value, None
        if adc is None:
            return None, None
        adc = int(adc)
        return adc, round(float(volts), 3) if volts is not None else round(adc / 1023 * 5.0, 3)

    @staticmethod
    def _door(closed: Any) -> Optional[str]:
        return None if closed is None else ("CLOSED" if closed else "OPEN")

    @staticmethod
    def _lock(enabled: Any) -> Optional[str]:
        return None if enabled is None else ("LOCKED" if enabled else "UNLOCKED")

    def _common(self) -> dict[str, Any]:
        return {
            "online": True,
            "fans_pwm": self._list("pwm", self.FANS, int),
            "fans_rpm": self._list("rpm", self.FANS, int),
            "doors": [self._door(v) for v in self._list("door", self.DOORS)],
        }

    def read(self) -> Optional[dict[str, Any]]:
        if self.client is None:
            return None
        if not self.client.is_connected():
            return {"online": False}
        return self._read()

    def _read(self) -> dict[str, Any]:
        raise NotImplementedError

    def _fan_pwm(self, value: Any) -> tuple[int, int]:
        if not isinstance(value, dict):
            raise RealCommandError(f'set_fan_pwm: ожидалось {{"fan": 0..{self.FANS - 1}, "pwm": 0..255}}')
        return (int(_num(value.get("fan"), 0, self.FANS - 1, "Индекс вентилятора")),
                int(_num(value.get("pwm"), 0, 255, "PWM")))

    def _command(self, action: str, value: Any) -> tuple:
        raise NotImplementedError

    async def execute(self, action: str, value: Any = None) -> None:
        if self.client is None or not self.client.is_connected():
            raise RealCommandError(f"{self.title}: стенд недоступен")
        if action == "set_door":
            raise RealCommandError("Двери стенда открывает Оператор руками — состояние видно по концевикам")
        call = self._command(action, value)
        if not await asyncio.to_thread(*call):
            raise RealCommandError(f"{self.title}: стенд не выполнил {action}")


class FabLabGreenhouseAdapter(_GreenhouseAdapter):
    node_id = "thermal_insulation_01"
    title = "Теплица ФабЛаб (МС-ТЮК)"
    module_name = "TYC.fablab_client"
    client_class = "FabLabClient"
    FANS = 6

    def _read(self) -> dict[str, Any]:
        dust_adc, dust_v = self._adc(self._v("dust"))
        mode = self._v("mode")
        if isinstance(mode, dict):
            mode = mode.get("mode")
        vacuum = self._v("vacuum")
        updates = {
            **self._common(),
            "temperature_c": self._list("temp", 3, float),
            "pressure_pa": self._list("pressure", 3, float),
            "humidity_pct": self._list("humidity", 3, float),
            "voc_index": self._list("voc", 2, int),
            "dust_adc": dust_adc,
            "dust_voltage_v": dust_v,
            "locks": [self._lock(v) for v in self._list("lock", self.DOORS)],
            "vacuum_on": None if vacuum is None else bool(vacuum),
        }
        if isinstance(mode, int):
            updates["fault_mode"] = mode
        return updates

    def _command(self, action: str, value: Any) -> tuple:
        c = self.client
        if action == "set_fan_pwm":
            return (c.set_pwm, *self._fan_pwm(value))
        if action == "set_lock":
            if not isinstance(value, dict):
                raise RealCommandError('set_lock: ожидалось {"lock": 0..1, "locked": true|false}')
            idx = int(_num(value.get("lock"), 0, self.DOORS - 1, "Индекс замка"))
            return (c.set_door_lock, idx, _bool(value.get("locked", True), "locked"))
        if action == "set_vacuum":
            mode = value.get("mode") if isinstance(value, dict) else value
            return (c.set_vacuum, 1 if _bool(mode, "set_vacuum") else 0)
        if action == "set_fault_mode":
            if not isinstance(value, dict):
                raise RealCommandError('set_fault_mode: ожидалось {"mode": 0..2, "fan": 0..5}')
            return (c.set_mode, int(_num(value.get("mode"), 0, 2, "Режим неисправности")),
                    int(_num(value.get("fan", 0), 0, self.FANS - 1, "Индекс вентилятора")))
        raise RealCommandError(f"Теплица ФабЛаб не поддерживает {action!r} "
                               "(доступно: set_fan_pwm, set_lock, set_vacuum, set_fault_mode, set_poll_interval)")


class ChemLabGreenhouseAdapter(_GreenhouseAdapter):
    node_id = "thermal_insulation_02"
    title = "Теплица Химлаб (МС-ТЮК)"
    module_name = "TYC.chem_reactor_client"
    client_class = "ChemReactorClient"
    FANS = 2
    RELAY_LEVELS = 10

    def _read(self) -> dict[str, Any]:
        gas = [self._adc(v) if v is not None else (None, None) for v in self._list("gas", 2)]
        level = self._v("relay_level")
        return {
            **self._common(),
            "temperature_c": self._list("temp", 2, float),
            "pressure_pa": self._list("pressure", 2, float),
            "humidity_pct": self._list("humidity", 2, float),
            "gas_adc": [g[0] for g in gas],
            "gas_voltage_v": [g[1] for g in gas],
            "lock": self._lock(self._v("lock_state")),
            "relay_fan_level": None if level is None else int(level),
        }

    def _command(self, action: str, value: Any) -> tuple:
        c = self.client
        if action == "set_fan_pwm":
            return (c.set_pwm, *self._fan_pwm(value))
        if action == "set_relay_fan_level":
            level = value.get("level") if isinstance(value, dict) else value
            return (c.set_relay_level, int(_num(level, 0, self.RELAY_LEVELS, "Уровень релейного вентилятора")))
        if action == "set_lock":
            locked = value.get("locked") if isinstance(value, dict) else value
            return (c.set_door_lock, _bool(locked, "set_lock"))
        raise RealCommandError(f"Теплица Химлаб не поддерживает {action!r} "
                               "(доступно: set_fan_pwm, set_relay_fan_level, set_lock)")


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
    Хосты принтера и фрезера — PRINTER_HOST / CNC_HOST, адреса теплиц МС-ТЮК —
    FABLAB_URL / CHEMLAB_URL; пустое значение отключает устройство.
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

    fablab_url = os.getenv("FABLAB_URL", "http://192.168.8.36:8000").strip()
    if fablab_url and wanted("thermal_insulation_01"):
        adapters.append(FabLabGreenhouseAdapter(fablab_url))

    chemlab_url = os.getenv("CHEMLAB_URL", "http://192.168.8.46:8001").strip()
    if chemlab_url and wanted("thermal_insulation_02"):
        adapters.append(ChemLabGreenhouseAdapter(chemlab_url))

    return {a.node_id: a for a in adapters if wanted(a.node_id)}

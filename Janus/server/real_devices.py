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
и ещё не проверены на реальном оборудовании.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
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

class CNCAdapter(RealDeviceAdapter):
    """
    start_job: value = имя файла (или {"file_name": ...}) внутри каталога
    gcode_dir — произвольные пути с сервера запрещены.
    Контроллер не умеет abort / reset_alarm / произвольный G-code — такие
    команды отклоняются. Дополнительно: probe_z (поиск нуля Z пробником).
    """
    node_id = "cnc_01"
    title = "ЧПУ-фрезер 3018 Pro (GRBL)"
    RETRY_S = 30.0

    def __init__(self, host: str, port: int = 8881, gcode_dir: Optional[Path] = None) -> None:
        self.host, self.port = host, port
        self.gcode_dir = (gcode_dir or SERVER_DIR / "frezer" / "gcode").resolve()
        self._module = _import_optional("cnc_controller", SERVER_DIR / "frezer")
        self.cnc = None
        self._last_attempt = float("-inf")

    def start(self) -> None:
        self.ensure_started()

    def ensure_started(self) -> None:
        """Контроллер падает на старте без связи — повторяем попытку раз в RETRY_S."""
        import time
        if self._module is None or (self.cnc is not None and self.cnc.is_running()):
            return
        if time.monotonic() - self._last_attempt < self.RETRY_S:
            return
        self._last_attempt = time.monotonic()
        cnc = self._module.CNCController(ip=self.host, port=self.port)
        try:
            cnc.start()
        except Exception as e:
            logger.warning("ЧПУ %s:%d недоступен: %s", self.host, self.port, e)
            return
        self.cnc = cnc
        logger.info("CNCController %s:%d запущен", self.host, self.port)

    def stop(self) -> None:
        if self.cnc is not None:
            self.cnc.stop()
            self.cnc = None

    def available(self) -> bool:
        return self.cnc is not None and self.cnc.get_state().online

    def read(self) -> Optional[dict[str, Any]]:
        self.ensure_started()
        if self.cnc is None:
            return None
        s = self.cnc.get_state()
        if not s.online:
            return {"online": False}
        if s.milling:
            state = "PAUSED" if s.paused else "RUNNING"
        else:
            state = "IDLE"
        return {
            "online": True,
            "state": state,
            "paused": s.paused,
            "x_mm": round(s.x, 3), "y_mm": round(s.y, 3), "z_mm": round(s.z, 3),
            "spindle_rpm": s.spindle_rpm if s.spindle_on else 0.0,
            "file_name": Path(s.current_file).name if s.current_file else None,
            "progress_pct": round(s.progress, 1),
            "error_code": None,
            "error_text": s.error,
        }

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

    async def execute(self, action: str, value: Any = None) -> None:
        c = self.cnc
        if c is None:
            raise RealCommandError("Фрезер не подключен")
        if action == "feed_hold":
            call = (c.pause,)
        elif action == "resume":
            call = (c.resume,)
        elif action == "home":
            call = (c.home,)
        elif action == "start_job":
            call = (c.start_milling, self._job_path(value))
        elif action == "probe_z":
            call = (c.probe_z_zero,)
        else:
            raise RealCommandError(f"Реальный фрезер не поддерживает {action!r} "
                                   "(доступно: feed_hold, resume, home, start_job, probe_z)")
        try:
            await asyncio.to_thread(*call)
        except RealCommandError:
            raise
        except Exception as e:  # CNCBusyError, CNCConnectionError, ...
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

    printer_host = os.getenv("PRINTER_HOST", "192.168.3.12").strip()
    if printer_host and wanted("printer_3d_01"):
        port = os.getenv("PRINTER_PORT", "").strip()
        adapters.append(PrinterAdapter(printer_host, int(port) if port else None))

    cnc_host = os.getenv("CNC_HOST", "172.31.170.108").strip()
    if cnc_host and wanted("cnc_01"):
        gcode_dir = os.getenv("CNC_GCODE_DIR", "").strip()
        adapters.append(CNCAdapter(cnc_host, int(os.getenv("CNC_PORT", "8881")),
                                   Path(gcode_dir) if gcode_dir else None))

    return {a.node_id: a for a in adapters if wanted(a.node_id)}

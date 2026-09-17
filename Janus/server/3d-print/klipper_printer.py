from __future__ import annotations

import threading
from dataclasses import dataclass, asdict
from typing import Optional

import requests


@dataclass
class PrinterStatus:
    """Текущее состояние 3D-принтера."""

    # ------------------------------------------------------------
    # Соединение
    # ------------------------------------------------------------

    online: bool = False

    # ------------------------------------------------------------
    # Klipper
    # ------------------------------------------------------------

    klippy_connected: bool = False
    klippy_state: str = "unknown"
    klippy_message: str = ""

    # ------------------------------------------------------------
    # Печать
    # ------------------------------------------------------------

    print_state: str = "unknown"
    printing: bool = False
    paused: bool = False

    filename: Optional[str] = None
    progress: float = 0.0

    # ------------------------------------------------------------
    # Температуры
    # ------------------------------------------------------------

    extruder_temperature: Optional[float] = None
    extruder_target: Optional[float] = None

    bed_temperature: Optional[float] = None
    bed_target: Optional[float] = None

    # ------------------------------------------------------------
    # Ошибка
    # ------------------------------------------------------------

    error: Optional[str] = None


class KlipperPrinter:
    """
    Управление 3D-принтером через Moonraker API.

    Мониторинг состояния выполняется в отдельном потоке.
    """

    def __init__(
        self,
        host: str,
        port: Optional[int] = None,
        poll_interval: float = 1.0,
        request_timeout: float = 3.0,
    ):
        if port is None:
            self.base_url = f"http://{host}"
        else:
            self.base_url = f"http://{host}:{port}"

        self.poll_interval = poll_interval
        self.request_timeout = request_timeout

        self._status = PrinterStatus()

        self._status_lock = threading.Lock()
        self._stop_event = threading.Event()

        self._thread: Optional[threading.Thread] = None

        self._session = requests.Session()

    # ============================================================
    # Управление потоком
    # ============================================================

    def start(self) -> None:
        """Запустить фоновый мониторинг."""

        if self.is_running():
            return

        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="KlipperMonitor",
            daemon=True,
        )

        self._thread.start()

    def stop(self) -> None:
        """Остановить фоновый мониторинг."""

        self._stop_event.set()

        if self._thread is not None:
            self._thread.join(
                timeout=self.request_timeout + 1.0
            )

            self._thread = None

        self._session.close()

    def is_running(self) -> bool:
        """Проверить, запущен ли поток мониторинга."""

        return (
            self._thread is not None
            and self._thread.is_alive()
        )

    # ============================================================
    # Получение состояния
    # ============================================================

    def get_status(self) -> PrinterStatus:
        """
        Получить копию последнего известного состояния.
        """

        with self._status_lock:
            return PrinterStatus(
                **asdict(self._status)
            )

    # ============================================================
    # Управление принтером
    # ============================================================

    def home(
        self,
        axes: Optional[str] = None,
    ) -> bool:
        """
        Поиск нулевого положения.

        home()      -> G28
        home("X")   -> G28 X
        home("XY")  -> G28 X Y
        home("XYZ") -> G28 X Y Z
        """

        if axes is None:
            command = "G28"

        else:
            axes = axes.upper()

            if not axes:
                raise ValueError(
                    "Необходимо указать хотя бы одну ось"
                )

            if any(axis not in "XYZ" for axis in axes):
                raise ValueError(
                    "Допустимые оси: X, Y, Z"
                )

            command = "G28 " + " ".join(axes)

        return self.send_gcode(command)

    def pause(self) -> bool:
        """Поставить печать на паузу."""

        return self._simple_command("pause")

    def resume(self) -> bool:
        """Продолжить печать."""

        return self._simple_command("resume")

    def cancel_print(self) -> bool:
        """Отменить текущую печать."""

        return self._simple_command("cancel")

    def send_gcode(
        self,
        command: str,
    ) -> bool:
        """Отправить произвольную G-code команду."""

        if not command or not command.strip():
            raise ValueError(
                "G-code команда не может быть пустой"
            )

        try:
            response = self._session.post(
                f"{self.base_url}/printer/gcode/script",
                json={
                    "script": command
                },
                timeout=self.request_timeout,
            )

            response.raise_for_status()

            return True

        except requests.RequestException as exc:
            self._set_error(str(exc))
            return False

    # ============================================================
    # Внутренняя реализация
    # ============================================================

    def _simple_command(
        self,
        command: str,
    ) -> bool:
        """Выполнить стандартную команду Moonraker."""

        try:
            response = self._session.post(
                f"{self.base_url}/printer/print/{command}",
                timeout=self.request_timeout,
            )

            response.raise_for_status()

            return True

        except requests.RequestException as exc:
            self._set_error(str(exc))
            return False

    def _monitor_loop(self) -> None:
        """Цикл фонового мониторинга."""

        while not self._stop_event.is_set():

            try:
                self._update_status()

            except requests.RequestException as exc:
                self._set_offline(str(exc))

            except Exception as exc:
                self._set_error(str(exc))

            self._stop_event.wait(
                self.poll_interval
            )

    def _update_status(self) -> None:
        """
        Получить актуальное состояние принтера.

        Запрашиваем:
        - print_stats
        - virtual_sdcard
        - pause_resume
        - webhooks
        - extruder
        - heater_bed
        """

        response = self._session.get(
            f"{self.base_url}/printer/objects/query",
            params={
                "print_stats": "",
                "virtual_sdcard": "",
                "pause_resume": "",
                "webhooks": "",
                "extruder": "",
                "heater_bed": "",
            },
            timeout=self.request_timeout,
        )

        response.raise_for_status()

        data = response.json()

        result = data.get("result", {})
        status = result.get("status", {})

        # ========================================================
        # Получаем отдельные объекты
        # ========================================================

        print_stats = status.get(
            "print_stats",
            {},
        )

        virtual_sdcard = status.get(
            "virtual_sdcard",
            {},
        )

        pause_resume = status.get(
            "pause_resume",
            {},
        )

        webhooks = status.get(
            "webhooks",
            {},
        )

        extruder = status.get(
            "extruder",
            {},
        )

        heater_bed = status.get(
            "heater_bed",
            {},
        )

        # ========================================================
        # Klipper
        # ========================================================

        klippy_state = webhooks.get(
            "state",
            "unknown",
        )

        klippy_message = webhooks.get(
            "state_message",
            "",
        )

        klippy_connected = (
            klippy_state == "ready"
        )

        # ========================================================
        # Печать
        # ========================================================

        print_state = print_stats.get(
            "state",
            "unknown",
        )

        paused = bool(
            pause_resume.get(
                "is_paused",
                False,
            )
        )

        # Главное:
        # состояние printing определяется по print_stats.state.
        #
        # Если Klipper сообщает printing,
        # печать идет независимо от progress.

        printing = (
            print_state == "printing"
        )

        # ========================================================
        # Файл и прогресс
        # ========================================================

        filename = print_stats.get(
            "filename"
        )

        progress = virtual_sdcard.get(
            "progress",
            0.0,
        )

        if progress is None:
            progress = 0.0

        # ========================================================
        # Температура экструдера
        # ========================================================

        extruder_temperature = extruder.get(
            "temperature"
        )

        extruder_target = extruder.get(
            "target"
        )

        # ========================================================
        # Температура стола
        # ========================================================

        bed_temperature = heater_bed.get(
            "temperature"
        )

        bed_target = heater_bed.get(
            "target"
        )

        # ========================================================
        # Обновляем состояние
        # ========================================================

        with self._status_lock:

            # Если HTTP-запрос успешно выполнен,
            # Moonraker точно доступен.
            self._status.online = True

            self._status.klippy_connected = (
                klippy_connected
            )

            self._status.klippy_state = (
                klippy_state
            )

            self._status.klippy_message = (
                klippy_message
            )

            self._status.print_state = (
                print_state
            )

            self._status.printing = (
                printing
            )

            self._status.paused = (
                paused
            )

            self._status.filename = (
                filename
            )

            self._status.progress = (
                float(progress)
            )

            self._status.extruder_temperature = (
                self._to_float(extruder_temperature)
            )

            self._status.extruder_target = (
                self._to_float(extruder_target)
            )

            self._status.bed_temperature = (
                self._to_float(bed_temperature)
            )

            self._status.bed_target = (
                self._to_float(bed_target)
            )

            self._status.error = None

    # ============================================================
    # Вспомогательные методы
    # ============================================================

    @staticmethod
    def _to_float(
        value,
    ) -> Optional[float]:
        """Безопасно преобразовать значение в float."""

        if value is None:
            return None

        try:
            return float(value)

        except (TypeError, ValueError):
            return None

    def _set_offline(
        self,
        error: str,
    ) -> None:
        """Зафиксировать потерю соединения."""

        with self._status_lock:

            self._status.online = False
            self._status.klippy_connected = False
            self._status.error = error

    def _set_error(
        self,
        error: str,
    ) -> None:
        """Зафиксировать ошибку."""

        with self._status_lock:
            self._status.error = error
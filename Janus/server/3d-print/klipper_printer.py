from __future__ import annotations

import os
import threading
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

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
    # Движение и позиция
    # ------------------------------------------------------------

    homing: bool = False
    toolhead_status: str = "unknown"

    position_x: Optional[float] = None
    position_y: Optional[float] = None
    position_z: Optional[float] = None
    position_e: Optional[float] = None

    # ------------------------------------------------------------
    # Скорость печати
    # ------------------------------------------------------------

    speed_factor: float = 1.0  # Множитель скорости (1.0 = 100%)

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
        
        Примечание: Команда выполняется асинхронно.
        Используйте status.homing для отслеживания процесса.
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

        return self._send_gcode_async(command)

    def pause(self) -> bool:
        """Поставить печать на паузу."""

        return self._simple_command("pause")

    def resume(self) -> bool:
        """Продолжить печать."""

        return self._simple_command("resume")

    def cancel_print(self) -> bool:
        """Отменить текущую печать."""

        return self._simple_command("cancel")

    # ============================================================
    # Управление файлами
    # ============================================================

    def list_files(
        self,
        root: str = "gcodes",
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Получить список файлов в указанной директории.
        
        Args:
            root: Корневая директория ("gcodes", "config", и т.д.)
        
        Returns:
            Список файлов с информацией (path, size, modified) или None при ошибке
        """

        try:
            response = self._session.get(
                f"{self.base_url}/server/files/list",
                params={"root": root},
                timeout=self.request_timeout,
            )

            response.raise_for_status()
            data = response.json()

            return data.get("result", [])

        except requests.RequestException as exc:
            self._set_error(f"Ошибка получения списка файлов: {exc}")
            return None

    def get_file_metadata(
        self,
        filename: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Получить метаданные файла (время печати, расход пластика и т.д.).
        
        Args:
            filename: Имя файла (путь относительно gcodes/)
        
        Returns:
            Словарь с метаданными или None при ошибке
        """

        try:
            response = self._session.get(
                f"{self.base_url}/server/files/metadata",
                params={"filename": filename},
                timeout=self.request_timeout,
            )

            response.raise_for_status()
            data = response.json()

            return data.get("result", {})

        except requests.RequestException as exc:
            self._set_error(f"Ошибка получения метаданных файла '{filename}': {exc}")
            return None

    def upload_file(
        self,
        file_path: str,
        filename: Optional[str] = None,
        root: str = "gcodes",
    ) -> bool:
        """
        Загрузить G-code файл на принтер.
        
        Args:
            file_path: Путь к локальному файлу
            filename: Имя файла на принтере (если None, используется имя из file_path)
            root: Целевая директория (по умолчанию "gcodes")
        
        Returns:
            True при успехе, False при ошибке
        """

        if not os.path.exists(file_path):
            self._set_error(f"Файл не найден: {file_path}")
            return False

        if not os.path.isfile(file_path):
            self._set_error(f"Указанный путь не является файлом: {file_path}")
            return False

        # Используем имя файла из пути, если не указано
        if filename is None:
            filename = os.path.basename(file_path)

        try:
            with open(file_path, "rb") as f:
                files = {
                    "file": (filename, f, "application/octet-stream")
                }

                response = self._session.post(
                    f"{self.base_url}/server/files/upload",
                    files=files,
                    data={"root": root},
                    timeout=30.0,  # Увеличенный таймаут для загрузки
                )

                response.raise_for_status()

            return True

        except requests.RequestException as exc:
            self._set_error(f"Ошибка загрузки файла '{filename}': {exc}")
            return False

        except Exception as exc:
            self._set_error(f"Неожиданная ошибка при загрузке файла '{filename}': {exc}")
            return False

    def delete_file(
        self,
        filename: str,
    ) -> bool:
        """
        Удалить файл с принтера.
        
        Args:
            filename: Имя файла для удаления (путь относительно gcodes/)
        
        Returns:
            True при успехе, False при ошибке
        """

        # Проверяем, что не пытаемся удалить файл во время печати
        status = self.get_status()
        if status.printing and status.filename == filename:
            self._set_error(f"Невозможно удалить файл '{filename}': файл печатается")
            return False

        try:
            response = self._session.delete(
                f"{self.base_url}/server/files/gcodes/{filename}",
                timeout=self.request_timeout,
            )

            response.raise_for_status()

            return True

        except requests.RequestException as exc:
            self._set_error(f"Ошибка удаления файла '{filename}': {exc}")
            return False

    def start_print(
        self,
        filename: str,
    ) -> bool:
        """
        Запустить печать указанного файла.
        
        Args:
            filename: Имя файла для печати (путь относительно gcodes/)
        
        Returns:
            True при успехе, False при ошибке
        """

        # Проверяем состояние принтера
        status = self.get_status()

        if not status.online:
            self._set_error("Принтер не в сети")
            return False

        if not status.klippy_connected:
            self._set_error("Klipper не подключен")
            return False

        if status.klippy_state != "ready":
            self._set_error(f"Принтер не готов: {status.klippy_state}")
            return False

        if status.printing:
            self._set_error("Печать уже выполняется")
            return False

        try:
            # Используем короткий таймаут, так как команда может выполняться долго
            # (нагрев сопла/стола может занять минуты)
            response = self._session.post(
                f"{self.base_url}/printer/print/start",
                json={"filename": filename},
                timeout=1.0,  # Короткий таймаут - команда принята, но выполняется асинхронно
            )

            # Если успели получить ответ - отлично
            if response.status_code == 200:
                return True
                
            response.raise_for_status()
            return True

        except requests.Timeout:
            # Timeout - это нормально для start_print
            # Команда уже отправлена и принтер начинает процесс
            return True

        except requests.RequestException as exc:
            # Реальная ошибка (соединение, 4xx/5xx код и т.д.)
            error_msg = f"Ошибка запуска печати '{filename}': {exc}"
            self._set_error(error_msg)
            return False

    # ============================================================
    # Управление скоростью печати
    # ============================================================

    def set_print_speed(
        self,
        factor: float,
    ) -> bool:
        """
        Изменить скорость печати.
        
        Args:
            factor: Множитель скорости (0.5 = 50%, 1.0 = 100%, 2.0 = 200%)
        
        Returns:
            True при успехе, False при ошибке
        
        Примечание: Безопасный диапазон 0.5-2.0 (50%-200%)
        """

        if factor < 0.5 or factor > 2.0:
            self._set_error(
                f"Недопустимое значение скорости: {factor}. "
                f"Допустимый диапазон: 0.5-2.0 (50%-200%)"
            )
            return False

        # Конвертируем в проценты для G-code (M220 S100 = 100%)
        speed_percent = int(factor * 100)

        # Отправляем G-code команду M220
        command = f"M220 S{speed_percent}"
        
        return self.send_gcode(command)

    def get_print_speed(self) -> float:
        """
        Получить текущую скорость печати.
        
        Returns:
            Множитель скорости (1.0 = 100%)
        """

        status = self.get_status()
        return status.speed_factor

    # ============================================================
    # Управление движением
    # ============================================================

    def move_relative(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        z: Optional[float] = None,
        feedrate: int = 3000,
    ) -> bool:
        """
        Относительное перемещение по осям.
        
        Args:
            x: Смещение по оси X в мм (макс. ±50мм)
            y: Смещение по оси Y в мм (макс. ±50мм)
            z: Смещение по оси Z в мм (макс. ±50мм)
            feedrate: Скорость движения в мм/мин (по умолчанию 3000)
        
        Returns:
            True при успехе, False при ошибке
        """

        status = self.get_status()
        if status.printing:
            self._set_error("Нельзя перемещать оси во время печати")
            return False

        if x is None and y is None and z is None:
            self._set_error("Необходимо указать хотя бы одну ось для перемещения")
            return False

        # Проверяем безопасные ограничения (±50мм)
        max_move = 50.0
        
        if x is not None and abs(x) > max_move:
            self._set_error(f"Перемещение по X ({x}мм) превышает безопасный лимит ±{max_move}мм")
            return False
        
        if y is not None and abs(y) > max_move:
            self._set_error(f"Перемещение по Y ({y}мм) превышает безопасный лимит ±{max_move}мм")
            return False
        
        if z is not None and abs(z) > max_move:
            self._set_error(f"Перемещение по Z ({z}мм) превышает безопасный лимит ±{max_move}мм")
            return False

        # Формируем команды G-code
        commands = []
        
        # Переключаемся в относительный режим
        commands.append("G91")
        
        # Формируем команду движения
        move_cmd = "G1"
        if x is not None:
            move_cmd += f" X{x:.3f}"
        if y is not None:
            move_cmd += f" Y{y:.3f}"
        if z is not None:
            move_cmd += f" Z{z:.3f}"
        move_cmd += f" F{feedrate}"
        
        commands.append(move_cmd)
        
        # Возвращаемся в абсолютный режим
        commands.append("G90")
        
        # Отправляем все команды одной строкой
        full_command = "\n".join(commands)

        return self._send_gcode_async(full_command)

    def get_position(self) -> Optional[Dict[str, float]]:
        """
        Получить текущую позицию toolhead.
        
        Returns:
            Словарь с координатами {"x": 0.0, "y": 0.0, "z": 0.0, "e": 0.0}
            или None если позиция недоступна
        """

        status = self.get_status()
        
        if (status.position_x is None or 
            status.position_y is None or 
            status.position_z is None):
            return None
        
        return {
            "x": status.position_x,
            "y": status.position_y,
            "z": status.position_z,
            "e": status.position_e if status.position_e is not None else 0.0,
        }

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
            self._set_error(f"Ошибка отправки G-code '{command}': {exc}")
            return False

    def _send_gcode_async(
        self,
        command: str,
    ) -> bool:
        """
        Отправить G-code команду без ожидания завершения.
        
        Используется для долгих команд (home, mesh calibration и т.д.),
        которые могут выполняться дольше request_timeout.
        """

        if not command or not command.strip():
            raise ValueError(
                "G-code команда не может быть пустой"
            )

        try:
            # Отправляем команду с минимальным таймаутом
            # Нам важно только убедиться, что команда принята,
            # а не дожидаться её выполнения
            response = self._session.post(
                f"{self.base_url}/printer/gcode/script",
                json={
                    "script": command
                },
                timeout=1.0,  # Короткий таймаут для проверки приема
            )

            # Если успели получить ответ - отлично
            if response.status_code == 200:
                return True
                
            response.raise_for_status()
            return True

        except requests.Timeout:
            # Timeout - это нормально для асинхронных команд
            # Команда уже отправлена и принята принтером
            return True
            
        except requests.RequestException as exc:
            # Реальная ошибка (соединение, 4xx/5xx код и т.д.)
            error_msg = f"Ошибка отправки асинхронной G-code '{command}': {exc}"
            self._set_error(error_msg)
            return False
        
        except Exception as exc:
            error_msg = f"Неожиданная ошибка при отправке асинхронной G-code '{command}': {exc}"
            self._set_error(error_msg)
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
        - toolhead (для отслеживания homing и позиции)
        - gcode_move (для отслеживания speed_factor)
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
                "toolhead": "",
                "gcode_move": "",
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

        toolhead = status.get(
            "toolhead",
            {},
        )

        gcode_move = status.get(
            "gcode_move",
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
        # Toolhead - статус и позиция
        # ========================================================

        # Получаем время выполнения команд
        print_time = toolhead.get("print_time", 0.0)
        estimated_print_time = toolhead.get("estimated_print_time", 0.0)
        
        # Определяем, выполняется ли движение (homing или печать)
        # Когда принтер активно выполняет команды, print_time отстает от estimated_print_time
        # При простое они почти равны (разница < 0.5 секунды)
        time_diff = abs(estimated_print_time - print_time)
        is_moving = time_diff > 0.5
        
        # Определяем homing: движение есть, но не печатаем
        homing = is_moving and not printing
        
        # Для отладки и совместимости сохраняем строковый статус
        if homing:
            toolhead_status = "Homing"
        elif printing:
            toolhead_status = "Printing"
        elif is_moving:
            toolhead_status = "Moving"
        else:
            toolhead_status = "Ready"

        # Получаем позицию [x, y, z, e]
        position = toolhead.get(
            "position",
            [None, None, None, None],
        )

        position_x = self._to_float(position[0]) if len(position) > 0 else None
        position_y = self._to_float(position[1]) if len(position) > 1 else None
        position_z = self._to_float(position[2]) if len(position) > 2 else None
        position_e = self._to_float(position[3]) if len(position) > 3 else None

        # ========================================================
        # Скорость печати из gcode_move
        # ========================================================

        speed_factor = gcode_move.get("speed_factor", 1.0)
        if speed_factor is None:
            speed_factor = 1.0

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

            self._status.homing = homing
            self._status.toolhead_status = toolhead_status

            self._status.position_x = position_x
            self._status.position_y = position_y
            self._status.position_z = position_z
            self._status.position_e = position_e

            self._status.speed_factor = float(speed_factor)

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
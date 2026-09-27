#!/usr/bin/env python3

from __future__ import annotations

import logging
import queue
import re
import socket
import threading
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ============================================================
# Logging
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# Exceptions
# ============================================================

class CNCError(Exception):
    """Базовая ошибка контроллера ЧПУ."""


class CNCConnectionError(CNCError):
    """Ошибка TCP-соединения."""


class CNCTimeoutError(CNCError):
    """Не удалось получить подтверждение."""


class CNCBusyError(CNCError):
    """Фрезер занят другой операцией."""


# ============================================================
# State
# ============================================================

@dataclass
class CNCState:
    """
    Текущее состояние фрезера.

    Пока координаты отслеживаются локально.
    Позже источник координат можно заменить на
    реальные данные GRBL.
    """

    # TCP
    online: bool = False

    # Работа
    milling: bool = False
    paused: bool = False

    # Прогресс
    progress: float = 0.0

    # Координаты
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    # Режим координат
    # True  -> G90
    # False -> G91
    absolute_mode: bool = True

    # Шпиндель
    spindle_on: bool = False

    # 0 = выключен
    # 3 = M3
    # 4 = M4
    spindle_direction: int = 0

    spindle_rpm: float = 0.0

    # Информация о файле
    current_file: Optional[str] = None
    current_line: int = 0
    total_lines: int = 0

    # Последняя ошибка
    error: Optional[str] = None


# ============================================================
# Internal task
# ============================================================

@dataclass
class _Task:
    """
    Внутренняя задача worker-потока.
    """

    command: str
    kind: str = "command"


# ============================================================
# CNC Controller
# ============================================================

class CNCController:
    """
    Управление CNC-фрезером через ESP32 TCP bridge.

    ESP32:
        TCP -> UART -> GRBL

    Дополнительный протокол подтверждения:

        ?OK
        -> OK:1

        !CLR
        -> сброс OK-флага

    Основное приложение работает в своём потоке.

    Все длительные операции выполняются в CNC worker thread.
    """

    def __init__(
        self,
        ip: str,
        port: int = 8881,

        socket_timeout: float = 2.0,

        # Максимальное время ожидания OK одной команды.
        ok_timeout: float = 30.0,

        # Интервал проверки ?OK.
        ok_poll_interval: float = 0.2,

        # Начальная задержка между попытками reconnect.
        reconnect_initial_delay: float = 1.0,

        # Максимальная задержка между reconnect.
        reconnect_max_delay: float = 10.0,
    ):
        self.ip = ip
        self.port = port

        self.socket_timeout = socket_timeout
        self.ok_timeout = ok_timeout
        self.ok_poll_interval = ok_poll_interval

        self.reconnect_initial_delay = reconnect_initial_delay
        self.reconnect_max_delay = reconnect_max_delay

        # ----------------------------------------------------
        # TCP socket
        # ----------------------------------------------------

        self._socket: Optional[socket.socket] = None

        # Защищает socket от одновременного использования
        # несколькими потоками.
        self._socket_lock = threading.Lock()

        # ----------------------------------------------------
        # Worker
        # ----------------------------------------------------

        self._worker: Optional[threading.Thread] = None

        # Полная остановка контроллера.
        self._stop_event = threading.Event()

        # Пауза фрезеровки.
        self._pause_event = threading.Event()

        # ----------------------------------------------------
        # Очередь отдельных команд
        # ----------------------------------------------------

        self._task_queue: queue.Queue[_Task] = queue.Queue()

        # ----------------------------------------------------
        # State
        # ----------------------------------------------------

        self._state = CNCState()

        self._state_lock = threading.Lock()

        # ----------------------------------------------------
        # G-code
        # ----------------------------------------------------

        self._gcode_file: Optional[Path] = None

        self._gcode_lines: list[str] = []

        # Индекс следующей команды.
        self._gcode_position: int = 0

        # Защита от одновременного запуска нескольких
        # фрезеровок.
        self._milling_lock = threading.Lock()

    # ========================================================
    # Public API
    # ========================================================

    def start(self) -> None:
        """
        Запускает контроллер.

        Создаёт worker-поток и устанавливает TCP-соединение.
        """

        if self.is_running():
            return

        self._stop_event.clear()
        self._pause_event.clear()

        # Первоначальное подключение.
        self._connect()

        self._worker = threading.Thread(
            target=self._worker_loop,
            name="CNCWorker",
            daemon=True,
        )

        self._worker.start()

        logger.info("CNC worker запущен")

    def stop(self) -> None:
        """
        Останавливает контроллер.

        Если выполняется фрезеровка, отправляется feed hold '!'.
        """

        logger.info("Остановка CNC controller")

        self._stop_event.set()

        # Снимаем состояние паузы, чтобы worker мог завершиться.
        self._pause_event.clear()

        # Пытаемся остановить движение GRBL.
        try:
            self._send_raw("!")
        except Exception:
            pass

        # Ждём worker.
        if self._worker is not None:

            self._worker.join(timeout=3.0)

            if self._worker.is_alive():
                logger.warning(
                    "Worker не завершился за отведённое время"
                )

        self._worker = None

        # Закрываем socket.
        self._close_socket()

        # Обновляем состояние.
        with self._state_lock:
            self._state.online = False
            self._state.milling = False
            self._state.paused = False

        logger.info("CNC controller остановлен")

    def start_milling(self, filename: str | Path) -> None:
        """
        Запускает фрезеровку файла.

        Метод НЕ блокирует основной поток.

        Файл полностью читается в память, после чего worker
        последовательно отправляет команды.
        """

        path = Path(filename)

        if not path.exists():
            raise FileNotFoundError(
                f"G-code файл не найден: {path}"
            )

        if not path.is_file():
            raise ValueError(
                f"Указанный путь не является файлом: {path}"
            )

        if not self.is_running():
            raise CNCError(
                "CNCController не запущен"
            )

        with self._milling_lock:

            with self._state_lock:
                if self._state.milling:
                    raise CNCBusyError(
                        "Фрезеровка уже выполняется"
                    )

            # Загружаем файл.
            lines = self._load_gcode(path)

            if not lines:
                raise ValueError(
                    "G-code файл не содержит команд"
                )

            # Подготавливаем новое задание.
            self._gcode_file = path
            self._gcode_lines = lines
            self._gcode_position = 0

            self._pause_event.clear()

            with self._state_lock:

                self._state.milling = True
                self._state.paused = False

                self._state.progress = 0.0

                self._state.current_file = str(path)

                self._state.current_line = 0
                self._state.total_lines = len(lines)

                self._state.error = None

        logger.info(
            "Фрезеровка запущена: %s, команд: %d",
            path,
            len(lines),
        )

    def pause(self) -> None:
        """
        Ставит текущую фрезеровку на паузу.

        ! -> GRBL feed hold.
        """

        with self._state_lock:

            if not self._state.milling:
                return

            if self._state.paused:
                return

        logger.info("Запрос паузы")

        # Feed hold GRBL.
        self._send_raw("!")

        self._pause_event.set()

        with self._state_lock:
            self._state.paused = True

        logger.info("Фрезеровка поставлена на паузу")

    def resume(self) -> None:
        """
        Продолжает фрезеровку.

        ~ -> GRBL resume.
        """

        with self._state_lock:

            if not self._state.milling:
                return

            if not self._state.paused:
                return

        logger.info("Продолжение фрезеровки")

        # Resume GRBL.
        self._send_raw("~")

        self._pause_event.clear()

        with self._state_lock:
            self._state.paused = False

        logger.info("Фрезеровка продолжена")

    def home(self) -> None:
        """
        Поиск нулевого положения по концевикам.

        Пока используется $H.
        """

        self._check_available_for_manual_command()

        self._task_queue.put(
            _Task(
                command="$H",
                kind="home",
            )
        )

        logger.info("Команда homing добавлена в очередь")

    def probe_z_zero(
        self,
        command: str = "G38.2 Z-10 F50",
    ) -> None:
        """
        Поиск нуля Z пробником.

        По умолчанию:

            G38.2 Z-10 F50

        Позже параметры можно изменить.
        """

        self._check_available_for_manual_command()

        self._task_queue.put(
            _Task(
                command=command,
                kind="probe",
            )
        )

        logger.info(
            "Команда Z probe добавлена в очередь: %s",
            command,
        )

    def get_state(self) -> CNCState:
        """
        Возвращает копию состояния.

        Возвращается копия, поэтому внешний код не может
        напрямую испортить внутреннее состояние controller.
        """

        with self._state_lock:

            return CNCState(
                online=self._state.online,

                milling=self._state.milling,
                paused=self._state.paused,

                progress=self._state.progress,

                x=self._state.x,
                y=self._state.y,
                z=self._state.z,

                absolute_mode=self._state.absolute_mode,

                spindle_on=self._state.spindle_on,
                spindle_direction=self._state.spindle_direction,
                spindle_rpm=self._state.spindle_rpm,

                current_file=self._state.current_file,

                current_line=self._state.current_line,
                total_lines=self._state.total_lines,

                error=self._state.error,
            )

    def is_running(self) -> bool:
        """
        Проверяет, запущен ли worker.
        """

        return (
            self._worker is not None
            and self._worker.is_alive()
        )

    # ========================================================
    # TCP connection
    # ========================================================

    def _connect(self) -> None:
        """
        Устанавливает TCP соединение с ESP32.
        """

        self._close_socket()

        logger.info(
            "Подключение к ESP32 %s:%d",
            self.ip,
            self.port,
        )

        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

        sock.settimeout(self.socket_timeout)

        try:
            sock.connect(
                (self.ip, self.port)
            )

        except (OSError, socket.error):
            sock.close()
            raise

        with self._socket_lock:
            self._socket = sock

        with self._state_lock:
            self._state.online = True
            self._state.error = None

        # Старый OK-флаг может остаться установленным.
        try:
            self._clear_ok_flag()

        except CNCConnectionError:
            self._close_socket()
            raise

        logger.info(
            "TCP-соединение с ESP32 установлено"
        )

    def _close_socket(self) -> None:
        """
        Безопасно закрывает TCP socket.
        """

        with self._socket_lock:
            sock = self._socket
            self._socket = None

        if sock is not None:

            try:
                sock.shutdown(
                    socket.SHUT_RDWR
                )
            except OSError:
                pass

            try:
                sock.close()
            except OSError:
                pass

        with self._state_lock:
            self._state.online = False

    def _ensure_connection(self) -> None:
        """
        Проверяет наличие socket.

        Если соединения нет — выполняется reconnect.
        """

        with self._socket_lock:
            sock = self._socket

        if sock is not None:
            return

        if not self._reconnect():
            raise CNCConnectionError(
                "Не удалось подключиться к ESP32"
            )

    def _reconnect(self) -> bool:
        """
        Восстанавливает TCP-соединение.

        Используется exponential backoff:

            1
            1.5
            2.25
            ...
            10
            10
            ...

        При успешном подключении возвращает True.
        """

        self._close_socket()

        delay = self.reconnect_initial_delay

        while not self._stop_event.is_set():

            logger.warning(
                "Попытка восстановления TCP "
                "%s:%d",
                self.ip,
                self.port,
            )

            try:

                self._connect()

                logger.info(
                    "TCP-соединение восстановлено"
                )

                return True

            except (
                OSError,
                socket.error,
                CNCConnectionError,
            ) as exc:

                logger.warning(
                    "Reconnect не удался: %s",
                    exc,
                )

                with self._state_lock:
                    self._state.online = False
                    self._state.error = str(exc)

                time.sleep(delay)

                delay = min(
                    delay * 1.5,
                    self.reconnect_max_delay,
                )

        return False

    # ========================================================
    # Worker
    # ========================================================

    def _worker_loop(self) -> None:
        """
        Главный цикл worker-потока.
        """

        logger.info(
            "CNC worker thread запущен"
        )

        while not self._stop_event.is_set():

            # ------------------------------------------------
            # Если выполняется G-code файл
            # ------------------------------------------------

            with self._state_lock:
                milling = self._state.milling

            if milling:

                try:
                    self._process_milling()

                except Exception as exc:

                    logger.exception(
                        "Ошибка фрезеровки: %s",
                        exc,
                    )

                    self._finish_milling(
                        error=str(exc)
                    )

                continue

            # ------------------------------------------------
            # Если фрезеровки нет — проверяем очередь задач
            # ------------------------------------------------

            try:

                task = self._task_queue.get(
                    timeout=0.1
                )

            except queue.Empty:
                continue

            if self._stop_event.is_set():
                break

            try:

                self._execute_task(task)

            except Exception as exc:

                logger.exception(
                    "Ошибка команды %s: %s",
                    task.command,
                    exc,
                )

                with self._state_lock:
                    self._state.error = str(exc)

        logger.info(
            "CNC worker thread завершён"
        )

    # ========================================================
    # Milling
    # ========================================================

    def _process_milling(self) -> None:
        """
        Выполняет ОДНУ G-code команду.

        Очень важно:

        команда удаляется из очереди только после получения OK.

        Поэтому при TCP reconnect текущая команда не
        теряется.
        """

        # --------------------------------------------
        # Пауза
        # --------------------------------------------

        if self._pause_event.is_set():
            time.sleep(0.05)
            return

        if self._stop_event.is_set():
            return

        # --------------------------------------------
        # Все команды выполнены
        # --------------------------------------------

        if self._gcode_position >= len(
            self._gcode_lines
        ):

            self._finish_milling()

            return

        # --------------------------------------------
        # Получаем текущую команду
        # --------------------------------------------

        line = self._gcode_lines[
            self._gcode_position
        ]

        line_number = self._gcode_position + 1

        logger.debug(
            "G-code %d/%d: %s",
            line_number,
            len(self._gcode_lines),
            line,
        )

        # --------------------------------------------
        # Отправляем команду
        # --------------------------------------------

        self._send_gcode_and_wait_ok(line)

        # --------------------------------------------
        # Команда подтверждена.
        #
        # Только теперь двигаем позицию файла.
        # --------------------------------------------

        self._update_state_from_gcode(line)

        self._gcode_position += 1

        with self._state_lock:

            self._state.current_line = (
                self._gcode_position
            )

            if self._state.total_lines > 0:

                self._state.progress = (
                    self._gcode_position
                    / self._state.total_lines
                    * 100.0
                )

    def _finish_milling(
        self,
        error: Optional[str] = None,
    ) -> None:
        """
        Завершает текущее задание.
        """

        with self._state_lock:

            self._state.milling = False
            self._state.paused = False

            if error is not None:
                self._state.error = error

            elif self._state.total_lines > 0:
                self._state.progress = 100.0

        self._pause_event.clear()

        self._gcode_file = None
        self._gcode_lines = []
        self._gcode_position = 0

        if error is None:

            logger.info(
                "Фрезеровка завершена"
            )

        else:

            logger.error(
                "Фрезеровка завершена с ошибкой: %s",
                error,
            )

    # ========================================================
    # Individual commands
    # ========================================================

    def _execute_task(
        self,
        task: _Task,
    ) -> None:
        """
        Выполняет отдельную команду:

            $H
            G38.2 ...
        """

        logger.info(
            "Выполнение команды: %s",
            task.command,
        )

        self._send_gcode_and_wait_ok(
            task.command
        )

        # --------------------------------------------
        # Homing
        # --------------------------------------------

        if task.kind == "home":

            with self._state_lock:

                # Пока предполагаем, что после homing
                # станок находится в машинном нуле.
                self._state.x = 0.0
                self._state.y = 0.0
                self._state.z = 0.0

                self._state.absolute_mode = True

            logger.info(
                "Homing завершён"
            )

        # --------------------------------------------
        # Z probe
        # --------------------------------------------

        elif task.kind == "probe":

            logger.info(
                "Z probe завершён"
            )

            # Реальное значение Z пока не меняем.
            #
            # После уточнения поведения G38.2
            # здесь добавим необходимую обработку.

    # ========================================================
    # G-code transmission
    # ========================================================

    def _send_gcode_and_wait_ok(
        self,
        command: str,
    ) -> None:
        """
        Отправляет одну G-code команду и ждёт OK.

        При потере TCP выполняется reconnect.

        ВАЖНО:

        Эта функция не переходит к следующей команде,
        пока текущая не будет подтверждена.
        """

        command = command.strip()

        if not command:
            return

        if not command.endswith("\n"):
            command += "\n"

        while not self._stop_event.is_set():

            try:

                # ----------------------------------------
                # Проверяем соединение
                # ----------------------------------------

                self._ensure_connection()

                # ----------------------------------------
                # Отправляем G-code
                # ----------------------------------------

                self._send_raw(command)

                # ----------------------------------------
                # Ждём OK
                # ----------------------------------------

                self._wait_for_ok(
                    self.ok_timeout
                )

                # ----------------------------------------
                # OK получен
                # ----------------------------------------

                self._clear_ok_flag()

                return

            except CNCConnectionError as exc:

                logger.warning(
                    "TCP-соединение потеряно "
                    "при выполнении команды %r: %s",
                    command.strip(),
                    exc,
                )

                with self._state_lock:

                    self._state.online = False
                    self._state.error = str(exc)

                # ----------------------------------------
                # Reconnect
                # ----------------------------------------

                if not self._reconnect():
                    continue

                # После reconnect цикл начинается
                # снова с ТОЙ ЖЕ команды.
                continue

            except CNCTimeoutError:

                # Таймаут OK — это уже не обязательно
                # проблема TCP.
                raise

        raise CNCError(
            "Выполнение команды остановлено"
        )

    def _wait_for_ok(
        self,
        timeout: float,
    ) -> bool:
        """
        Ждёт OK:1.

        Если TCP оборвался — выбрасывает
        CNCConnectionError, чтобы внешний код
        выполнил reconnect.
        """

        deadline = (
            time.monotonic()
            + timeout
        )

        while time.monotonic() < deadline:

            if self._stop_event.is_set():
                return False

            try:

                reply = self._query_ok()

                logger.debug(
                    "OK query response: %r",
                    reply,
                )

                if "OK:1" in reply:
                    return True

            except CNCConnectionError:
                raise

            time.sleep(
                self.ok_poll_interval
            )

        raise CNCTimeoutError(
            "Не получено OK за "
            f"{timeout:.1f} секунд"
        )

    # ========================================================
    # ESP32 protocol
    # ========================================================

    def _query_ok(self) -> str:
        """
        Проверяет OK-флаг ESP32.
        """

        return self._send_request(
            "?OK\n"
        )

    def _clear_ok_flag(self) -> str:
        """
        Сбрасывает OK-флаг ESP32.
        """

        return self._send_request(
            "!CLR\n"
        )

    # ========================================================
    # TCP send / receive
    # ========================================================

    def _send_raw(
        self,
        data: str,
    ) -> None:
        """
        Отправляет данные в ESP32.

        Не читает ответ.
        """

        encoded = data.encode(
            "utf-8"
        )

        with self._socket_lock:
            sock = self._socket

        if sock is None:

            raise CNCConnectionError(
                "Нет TCP-соединения с ESP32"
            )

        try:

            sock.sendall(
                encoded
            )

        except (
            OSError,
            socket.error,
        ) as exc:

            self._mark_connection_error(
                exc
            )

            self._close_socket()

            raise CNCConnectionError(
                f"Ошибка отправки TCP: {exc}"
            ) from exc

    def _send_request(
        self,
        data: str,
    ) -> str:
        """
        Отправляет запрос и читает ответ.

        Используется для:

            ?OK
            !CLR
        """

        # --------------------------------------------
        # Отправка
        # --------------------------------------------

        self._send_raw(data)

        # --------------------------------------------
        # Получаем socket
        # --------------------------------------------

        with self._socket_lock:
            sock = self._socket

        if sock is None:

            raise CNCConnectionError(
                "Нет TCP-соединения"
            )

        response = bytearray()

        old_timeout = sock.gettimeout()

        try:

            # Короткий timeout нужен потому,
            # что ответ может прийти несколькими
            # TCP пакетами.
            sock.settimeout(0.5)

            while True:

                try:

                    chunk = sock.recv(
                        1024
                    )

                except socket.timeout:

                    # Нет новых данных.
                    break

                except (
                    OSError,
                    socket.error,
                ) as exc:

                    self._mark_connection_error(
                        exc
                    )

                    self._close_socket()

                    raise CNCConnectionError(
                        f"Ошибка чтения TCP: {exc}"
                    ) from exc

                # ------------------------------------
                # ESP32 закрыл соединение
                # ------------------------------------

                if not chunk:

                    self._mark_connection_error(
                        ConnectionError(
                            "ESP32 закрыл "
                            "TCP-соединение"
                        )
                    )

                    self._close_socket()

                    raise CNCConnectionError(
                        "ESP32 закрыл "
                        "TCP-соединение"
                    )

                response.extend(
                    chunk
                )

        finally:

            try:
                sock.settimeout(
                    old_timeout
                )
            except OSError:
                pass

        return response.decode(
            "utf-8",
            errors="ignore",
        )

    def _mark_connection_error(
        self,
        exc: Exception,
    ) -> None:
        """
        Отмечает потерю соединения.
        """

        logger.error(
            "Соединение с ESP32 потеряно: %s",
            exc,
        )

        with self._state_lock:

            self._state.online = False
            self._state.error = str(exc)

    # ========================================================
    # G-code parsing
    # ========================================================

    @staticmethod
    def _remove_comments(
        line: str,
    ) -> str:
        """
        Удаляет комментарии G-code.

        Поддерживаются:

            ; comment

        и:

            (comment)
        """

        # Удаляем комментарии вида:
        #
        # (comment)
        #
        line = re.sub(
            r"\([^)]*\)",
            "",
            line,
        )

        # Удаляем:
        #
        # ; comment
        #
        line = line.split(
            ";",
            1,
        )[0]

        return line.strip()

    def _update_state_from_gcode(
        self,
        line: str,
    ) -> None:
        """
        Обновляет только те части состояния,
        которые пока умеем отслеживать.

        ВАЖНО:

        Этот parser НЕ решает, отправлять команду
        или нет.

        Любая команда из файла уже была отправлена
        до вызова этой функции.

        Поэтому G2/G3/G4/G38.x и любые другие
        команды не будут пропущены.
        """

        line = self._remove_comments(
            line
        )

        if not line:
            return

        upper = line.upper()

        # ----------------------------------------------------
        # G90
        # ----------------------------------------------------

        if re.search(
            r"\bG90\b",
            upper,
        ):

            with self._state_lock:
                self._state.absolute_mode = True

        # ----------------------------------------------------
        # G91
        # ----------------------------------------------------

        if re.search(
            r"\bG91\b",
            upper,
        ):

            with self._state_lock:
                self._state.absolute_mode = False

        # ----------------------------------------------------
        # Временное отслеживание координат
        # ----------------------------------------------------

        self._update_coordinates(
            upper
        )

        # ----------------------------------------------------
        # M3
        # ----------------------------------------------------

        if re.search(
            r"\bM3\b",
            upper,
        ):

            with self._state_lock:

                self._state.spindle_on = True
                self._state.spindle_direction = 3

        # ----------------------------------------------------
        # M4
        # ----------------------------------------------------

        if re.search(
            r"\bM4\b",
            upper,
        ):

            with self._state_lock:

                self._state.spindle_on = True
                self._state.spindle_direction = 4

        # ----------------------------------------------------
        # M5
        # ----------------------------------------------------

        if re.search(
            r"\bM5\b",
            upper,
        ):

            with self._state_lock:

                self._state.spindle_on = False
                self._state.spindle_direction = 0
                self._state.spindle_rpm = 0.0

        # ----------------------------------------------------
        # Sxxxx
        # ----------------------------------------------------

        spindle_match = re.search(
            r"\bS([-+]?\d+(?:\.\d+)?)",
            upper,
        )

        if spindle_match:

            rpm = float(
                spindle_match.group(1)
            )

            with self._state_lock:

                self._state.spindle_rpm = rpm

                # Если S используется вместе
                # с M3/M4 — считаем шпиндель включённым.
                if re.search(
                    r"\bM[34]\b",
                    upper,
                ):

                    self._state.spindle_on = True

    def _update_coordinates(
        self,
        line: str,
    ) -> None:
        """
        Временный расчёт координат.

        Позже этот метод можно полностью заменить
        получением реальных координат GRBL.

        Сейчас он нужен только для базового состояния.
        """

        coordinates: dict[str, float] = {}

        for axis in (
            "X",
            "Y",
            "Z",
        ):

            match = re.search(
                rf"\b{axis}"
                rf"([-+]?\d+(?:\.\d+)?)",
                line,
            )

            if match:

                coordinates[axis] = float(
                    match.group(1)
                )

        if not coordinates:
            return

        with self._state_lock:

            if self._state.absolute_mode:

                if "X" in coordinates:
                    self._state.x = coordinates["X"]

                if "Y" in coordinates:
                    self._state.y = coordinates["Y"]

                if "Z" in coordinates:
                    self._state.z = coordinates["Z"]

            else:

                if "X" in coordinates:
                    self._state.x += coordinates["X"]

                if "Y" in coordinates:
                    self._state.y += coordinates["Y"]

                if "Z" in coordinates:
                    self._state.z += coordinates["Z"]

    # ========================================================
    # G-code file
    # ========================================================

    def _load_gcode(
        self,
        path: Path,
    ) -> list[str]:
        """
        Загружает G-code файл.

        Комментарии и пустые строки удаляются.

        Никакой фильтрации по типу G-code команды нет.

        Например, все эти команды будут переданы:

            G0
            G1
            G2
            G3
            G4
            G38.2
            G90
            G91
            M3
            M5
            и т.д.
        """

        result: list[str] = []

        with path.open(
            "r",
            encoding="utf-8",
            errors="ignore",
        ) as file:

            for line in file:

                line = self._remove_comments(
                    line
                )

                if not line:
                    continue

                result.append(
                    line
                )

        return result

    # ========================================================
    # Validation
    # ========================================================

    def _check_available_for_manual_command(
        self,
    ) -> None:
        """
        Проверяет возможность запуска отдельной команды.
        """

        if not self.is_running():

            raise CNCError(
                "CNCController не запущен"
            )

        with self._state_lock:

            if self._state.milling:

                raise CNCBusyError(
                    "Нельзя выполнить команду: "
                    "сейчас идёт фрезеровка"
                )

            if not self._state.online:

                raise CNCConnectionError(
                    "Фрезер не подключен"
                )

    # ========================================================
    # Context manager
    # ========================================================

    def __enter__(self):
        self.start()
        return self

    def __exit__(
        self,
        exc_type,
        exc_val,
        exc_tb,
    ):
        self.stop()


# ============================================================
# Example
# ============================================================

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "[%(levelname)s] "
            "%(threadName)s: "
            "%(message)s"
        ),
    )

    cnc = CNCController(
        ip="172.31.170.108",
        port=8881,
    )

    try:

        cnc.start()

        cnc.start_milling(
            "board.nc"
        )

        # Основное приложение продолжает работать.
        while True:

            state = cnc.get_state()

            print(
                f"\r"
                f"Online: {state.online} | "
                f"Milling: {state.milling} | "
                f"Paused: {state.paused} | "
                f"Progress: {state.progress:.1f}% | "
                f"X={state.x:.3f} "
                f"Y={state.y:.3f} "
                f"Z={state.z:.3f} | "
                f"RPM={state.spindle_rpm:.0f}",
                end="",
                flush=True,
            )

            if (
                not state.milling
                and state.current_line > 0
            ):
                break

            time.sleep(0.5)

    except KeyboardInterrupt:

        print("\nОстановка...")

    finally:

        cnc.stop()
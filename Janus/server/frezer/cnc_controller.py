#!/usr/bin/env python3
"""
Управление ЧПУ-фрезером на GRBL 1.1 через ESP32 TCP <-> UART мост.

Архитектура
-----------
* IO-поток - ЕДИНСТВЕННЫЙ, кто читает из сокета. Разбирает вывод GRBL
  построчно:
      ok / error:N      -> подтверждение строки G-code
      <Idle|MPos:...>   -> отчёт о состоянии (реальные координаты)
      [PRB:x,y,z:1]     -> результат пробинга
      [GC:...]          -> модальное состояние ($G)
      ALARM:N           -> авария
      Grbl 1.1h ...     -> GRBL перезагрузился (soft reset)
  Каждые status_interval секунд IO-поток шлёт realtime-запрос '?'
  (БЕЗ перевода строки) и по отсутствию ответа определяет обрыв связи.
* Worker-поток выполняет задания (файл, $H, пробинг, ручные команды)
  по протоколу send-response: строка -> ждём ok/error -> следующая.
* Запись в сокет идёт под _send_lock, поэтому realtime-команды
  ('!', '~', 0x18) можно слать из любого потока в любой момент.

Фирменный протокол ESP32 (?OK / !CLR) больше НЕ используется: мост
прозрачно пересылает весь вывод GRBL, и ok/error читаются прямо из
потока. Файл работает и со старой прошивкой ESP32, и с исправленной.
"""

from __future__ import annotations

import collections
import logging
import queue
import re
import select
import socket
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Deque, Optional, Tuple


logger = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]

# Стандартные расширения gcode для фрезерных ЧПУ
GCODE_EXTENSIONS = {".gcode", ".nc", ".tap", ".ngc", ".gc", ".cnc"}
# ============================================================
# GRBL constants
# ============================================================

# В GRBL 1.1 LINE_BUFFER_SIZE = 80 (79 символов + терминатор).
# Пробелы и комментарии GRBL выкидывает ДО записи в буфер.
GRBL_MAX_LINE = 79

RT_STATUS = b"?"
RT_FEED_HOLD = b"!"
RT_CYCLE_START = b"~"
RT_SOFT_RESET = b"\x18"
RT_SPINDLE_STOP = b"\x9e"   # GRBL 1.1: Toggle Spindle Stop — только в состоянии Hold

# Состояния, в которых станок "что-то делает" - ожидание ok в них
# не ограничено по времени (ok может прийти через минуты, если
# буфер планировщика заполнен длинными медленными перемещениями).
_BUSY_STATES = ("Run", "Hold", "Jog", "Home", "Door")

GRBL_ERRORS = {
    1: "ожидалась буква команды",
    2: "неверный формат числа",
    3: "неверная $-команда",
    4: "отрицательное значение",
    5: "homing отключён ($22)",
    6: "минимальный импульс шага < 3 мкс",
    7: "ошибка чтения EEPROM",
    8: "$-команда только в Idle",
    9: "G-code заблокирован (Alarm или Jog) - нужен $H или $X",
    10: "soft limits требуют homing",
    11: "строка слишком длинная",
    12: "превышена частота шагов",
    13: "открыта дверь (safety door)",
    14: "строка слишком длинная для EEPROM",
    15: "jog выходит за пределы хода",
    16: "неверная jog-команда",
    17: "laser mode требует PWM",
    20: "неподдерживаемая команда",
    21: "конфликт модальной группы",
    22: "не задана подача F",
    23: "требуется целое значение",
    24: "несколько команд используют оси",
    25: "повтор слова в строке",
    26: "нет слов осей",
    27: "неверный номер строки",
    28: "нет обязательного значения",
    29: "G59.x не поддерживается",
    30: "G53 только с G0/G1",
    31: "лишние слова осей",
    32: "G2/G3: нет оси в плоскости",
    33: "недопустимая цель перемещения",
    34: "ошибка радиуса дуги",
    35: "G2/G3: нет смещения в плоскости",
    36: "лишние слова значений",
    37: "G43.1 не по оси инструмента",
    38: "номер инструмента слишком большой",
}

GRBL_ALARMS = {
    1: "сработал аппаратный концевик (hard limit)",
    2: "цель за пределами soft limits",
    3: "reset во время движения - позиция потеряна",
    4: "probe: пробник уже замкнут до начала движения",
    5: "probe: контакт не найден на всём ходе",
    6: "homing: reset во время цикла",
    7: "homing: открыта дверь",
    8: "homing: не удалось отъехать от концевика",
    9: "homing: концевик не найден",
}


# ============================================================
# Exceptions
# ============================================================

class CNCError(Exception):
    """Базовая ошибка контроллера ЧПУ."""


class CNCConnectionError(CNCError):
    """Ошибка или обрыв TCP-соединения."""


class CNCTimeoutError(CNCError):
    """Не дождались ответа."""


class CNCBusyError(CNCError):
    """Фрезер занят другой операцией."""


class CNCAbortedError(CNCError):
    """Операция прервана (abort / stop)."""


class CNCResetError(CNCError):
    """GRBL перезагрузился во время ожидания ответа."""


class CNCProbeError(CNCError):
    """Пробинг не удался."""


class CNCGrblError(CNCError):
    """GRBL ответил error:N."""

    def __init__(self, code: int, command: str):
        self.code = code
        self.command = command
        super().__init__(
            f"GRBL error:{code} ({GRBL_ERRORS.get(code, '?')}) "
            f"на команде {command!r}"
        )


class CNCAlarmError(CNCError):
    """GRBL перешёл в ALARM."""

    def __init__(self, code: Optional[int], hint: str = ""):
        self.code = code
        text = GRBL_ALARMS.get(code, "?") if code else "состояние Alarm"
        msg = f"GRBL ALARM:{code} ({text})" if code else f"GRBL {text}"
        if hint:
            msg += f". {hint}"
        super().__init__(msg)


# ============================================================
# State
# ============================================================

@dataclass(frozen=True)
class ProbeResult:
    """Результат [PRB:...]. Координаты - МАШИННЫЕ (MPos) точки касания."""

    x: float
    y: float
    z: float
    success: bool
    timestamp: float


@dataclass(frozen=True)
class GCodeFile:
    """Описание gcode-файла в рабочей директории."""
    name: str            # имя файла (basename)
    path: Path           # полный путь
    size: int            # байт
    mtime: float         # время последнего изменения (timestamp)
    valid: bool          # прошёл ли базовую проверку (длина строк, ASCII)
    error: Optional[str] = None  # причина, если valid=False

@dataclass
class CNCState:
    """Снимок состояния фрезера (координаты - реальные, из отчёта GRBL)."""

    # Связь
    online: bool = False

    # Задание
    milling: bool = False
    paused: bool = False
    progress: float = 0.0          # % подтверждённых строк (см. примечание)
    current_file: Optional[str] = None
    current_line: int = 0
    total_lines: int = 0

    # Рабочие координаты (WPos) - те, в которых написан G-code.
    # None, пока GRBL не прислал WCO (обычно в течение 1-3 c).
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None

    mpos: Optional[Vec3] = None     # машинные координаты
    wpos: Optional[Vec3] = None     # рабочие координаты
    wco: Optional[Vec3] = None      # смещение рабочей СК (WPos = MPos - WCO)

    # Из отчёта '?'
    machine_state: str = "Unknown"  # Idle, Run, Hold:0, Alarm, Home, ...
    feed_rate: float = 0.0          # текущая подача, мм/мин
    spindle_rpm: float = 0.0        # текущие обороты
    probe_pin: bool = False         # Pn:...P... - пробник замкнут
    pins: str = ""                  # вся строка Pn
    planner_free: Optional[int] = None
    rx_free: Optional[int] = None
    accessories: Optional[str] = None   # поле A: отчёта (S/C — шпиндель, F/M — СОЖ); None — ещё не было
    status_time: float = 0.0        # time.monotonic() последнего отчёта

    # Из $G
    absolute_mode: bool = True      # G90 / G91
    coord_system: str = "G54"
    spindle_direction: int = 0      # 0 = M5, 3 = M3, 4 = M4
    spindle_on: bool = False

    # Пробинг / ошибки
    last_probe: Optional[ProbeResult] = None
    alarm: Optional[int] = None
    error: Optional[str] = None


@dataclass
class _Task:
    kind: str
    payload: dict = field(default_factory=dict)
    future: Future = field(default_factory=Future)


# ============================================================
# CNC Controller
# ============================================================

class CNCController:
    """
    Управление GRBL-фрезером через ESP32 TCP bridge.

    Все длительные операции выполняются в worker-потоке; методы
    home(), unlock(), probe_z_zero(), send_command() возвращают
    concurrent.futures.Future - можно не ждать, а можно сделать
    .result(timeout=...).
    """

    def __init__(
        self,
        ip: str,
        port: int = 8881,
        *,
        gcode_dir: str | Path | None = None,
        connect_timeout: float = 3.0,
        # Как часто спрашивать '?' (GRBL рекомендует не чаще 5 Гц).
        status_interval: float = 0.2,
        # Нет ответа на '?' дольше этого - связь считается потерянной.
        status_timeout: float = 3.0,
        # Сколько ждать ok, если станок стоит (Idle) и ничего не делает.
        idle_ack_timeout: float = 20.0,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 10.0,
        
    ):
        self.ip = ip
        self.port = port
        self.connect_timeout = connect_timeout
        self.status_interval = status_interval
        self.status_timeout = status_timeout
        self.idle_ack_timeout = idle_ack_timeout
        self.reconnect_initial_delay = reconnect_initial_delay
        self.reconnect_max_delay = reconnect_max_delay

        # Один lock на всё разделяемое состояние + condition для ожиданий.
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)

        # Отдельный lock на запись в сокет. Порядок: _send_lock -> _lock,
        # никогда не наоборот (не вызывать _write() под _lock).
        self._send_lock = threading.Lock()

        self._sock: Optional[socket.socket] = None
        self._conn_gen = 0            # меняется при каждом connect/drop
        self._conn_time = 0.0
        self._rx_buf = bytearray()    # только IO-поток
        self._last_poll = 0.0         # только IO-поток
        self._last_status_rx = 0.0

        self._acks: Deque[str] = collections.deque()
        self._probe_seq = 0
        self._alarm_seq = 0
        self._reset_seq = 0
        self._wco: Optional[Vec3] = None

        self._state = CNCState()

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._abort_event = threading.Event()

        self._io_thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._tasks: "queue.Queue[_Task]" = queue.Queue()

        # Задание (используется только worker'ом после запуска)
        self._gcode_lines: list[str] = []
        self._gcode_position = 0
        self._milling_lock = threading.Lock()
        
        self.gcode_dir = Path(gcode_dir) if gcode_dir else None

    # ========================================================
    # Public API: жизненный цикл
    # ========================================================

    def start(self) -> None:
        """Подключается к ESP32 и запускает IO- и worker-потоки."""

        if self.is_running():
            return

        self._stop_event.clear()
        self._pause_event.clear()
        self._abort_event.clear()

        self._open_socket()   # бросит CNCConnectionError, если не вышло

        self._io_thread = threading.Thread(
            target=self._io_loop, name="CNC-IO", daemon=True
        )
        self._worker = threading.Thread(
            target=self._worker_loop, name="CNC-Worker", daemon=True
        )
        self._io_thread.start()
        self._worker.start()

        try:
            st = self.refresh_status(timeout=2.0)
            logger.info(
                "GRBL на связи: %s MPos=%s", st.machine_state, st.mpos
            )
        except CNCError:
            logger.warning(
                "GRBL не ответил на '?'. Проверьте UART/скорость/питание."
            )

    def stop(self) -> None:
        """
        Останавливает контроллер. Если идёт фрезеровка - безопасная
        остановка (feed hold -> дождаться остановки -> soft reset).
        """

        logger.info("Остановка CNC controller")

        with self._lock:
            milling = self._state.milling

        if milling and self.is_running():
            self._abort_event.set()
            deadline = time.monotonic() + 40.0
            while time.monotonic() < deadline:
                with self._lock:
                    if not self._state.milling:
                        break
                time.sleep(0.05)

        self._stop_event.set()

        for th in (self._worker, self._io_thread):
            if th is not None:
                th.join(timeout=3.0)
                if th.is_alive():
                    logger.warning("Поток %s не завершился", th.name)

        self._worker = None
        self._io_thread = None

        # Отменяем невыполненные задачи.
        while True:
            try:
                task = self._tasks.get_nowait()
            except queue.Empty:
                break
            task.future.cancel()

        self._drop_connection(None, "Контроллер остановлен", log=False)

        with self._lock:
            self._state.milling = False
            self._state.paused = False

        logger.info("CNC controller остановлен")

    def is_running(self) -> bool:
        return (
            self._worker is not None and self._worker.is_alive()
            and self._io_thread is not None and self._io_thread.is_alive()
        )

    # ========================================================
    # Public API: состояние и координаты
    # ========================================================

    def get_state(self) -> CNCState:
        """Копия текущего состояния (обновляется каждые status_interval)."""
        with self._lock:
            return replace(self._state)

    def get_position(self, work: bool = True) -> Optional[Vec3]:
        """
        Последние известные координаты.
        work=True  -> рабочие (WPos), work=False -> машинные (MPos).
        """
        with self._lock:
            return self._state.wpos if work else self._state.mpos

    def refresh_status(self, timeout: float = 1.0) -> CNCState:
        """
        Немедленно запрашивает '?' и ждёт СВЕЖИЙ отчёт.
        Можно вызывать из любого потока, в том числе во время фрезеровки.
        """
        t0 = time.monotonic()
        self._write(RT_STATUS)
        return self._wait_status(lambda s: True, timeout, "отчёта о статусе",
                                 since=t0)

    def wait_for_idle(self, timeout: float = 60.0) -> CNCState:
        """Ждёт, пока станок реально остановится (Idle)."""
        return self._wait_idle(timeout)

    # ========================================================
    # Public API: задание
    # ========================================================

    

    def list_gcode_files(
        self,
        directory: str | Path | None = None,
        *,
        recursive: bool = False,
        validate: bool = False,
    ) -> list[GCodeFile]:
        """
        Возвращает список gcode-файлов в директории.

        directory  - каталог для сканирования. Если None - используется
                    self.gcode_dir (см. п. 3), либо текущая папка.
        recursive  - обходить подкаталоги.
        validate   - если True, для каждого файла запустить проверку,
                    аналогичную _load_gcode (длина строк, ASCII).
                    Медленнее, но сразу отсеивает битые файлы.

        Файлы сортируются по времени изменения (новые сверху).
        """
        root = Path(directory) if directory is not None else (
            getattr(self, "gcode_dir", None) or Path.cwd()
        )
        if not root.is_dir():
            raise FileNotFoundError(f"Директория не найдена: {root}")

        iterator = root.rglob("*") if recursive else root.iterdir()
        result: list[GCodeFile] = []

        for path in iterator:
            if not path.is_file():
                continue
            if path.suffix.lower() not in GCODE_EXTENSIONS:
                continue
            # Защита от symlink-петель и файлов вне корня
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError:
                continue

            stat = path.stat()
            valid, err = True, None
            if validate:
                try:
                    self._load_gcode(path)   # уже есть в классе
                except Exception as exc:     # noqa: BLE001
                    valid, err = False, str(exc)

            result.append(GCodeFile(
                name=path.name,
                path=path,
                size=stat.st_size,
                mtime=stat.st_mtime,
                valid=valid,
                error=err,
            ))

        result.sort(key=lambda f: f.mtime, reverse=True)
        return result
    
    def start_milling(self, filename: str | Path) -> None:
        """Запускает фрезеровку файла (не блокирует вызывающий поток)."""

        path = Path(filename)
        if not path.is_file():
            raise FileNotFoundError(f"G-code файл не найден: {path}")

        self._check_ready(allow_alarm=False)

        lines = self._load_gcode(path)
        if not lines:
            raise ValueError("G-code файл не содержит команд")

        with self._milling_lock:
            with self._lock:
                if self._state.milling:
                    raise CNCBusyError("Фрезеровка уже выполняется")
                if not self._tasks.empty():
                    raise CNCBusyError("В очереди есть невыполненные команды")

                self._gcode_lines = lines
                self._gcode_position = 0
                self._pause_event.clear()
                self._abort_event.clear()

                self._state.milling = True
                self._state.paused = False
                self._state.progress = 0.0
                self._state.current_file = str(path)
                self._state.current_line = 0
                self._state.total_lines = len(lines)
                self._state.error = None

        logger.info("Фрезеровка запущена: %s, команд: %d", path, len(lines))

    def pause(self) -> None:
        """Feed hold ('!'). Станок плавно тормозит, позиция сохраняется."""
        with self._lock:
            if not self._state.milling or self._state.paused:
                return
        self._write(RT_FEED_HOLD)
        self._pause_event.set()
        with self._lock:
            self._state.paused = True
        logger.info("Фрезеровка на паузе")

    def resume(self) -> None:
        """Cycle start ('~')."""
        with self._lock:
            if not self._state.milling or not self._state.paused:
                return
        self._write(RT_CYCLE_START)
        self._pause_event.clear()
        with self._lock:
            self._state.paused = False
        logger.info("Фрезеровка продолжена")

    def spindle_stop(self) -> bool:
        """Остановка шпинделя на паузе (GRBL 1.1 Spindle Stop override, 0x9E).
        Работает только в Hold; на resume ('~') GRBL сам раскрутит шпиндель
        и продолжит. Команда переключающая — повторная отправка снова включит
        шпиндель, поэтому шлём её, только если шпиндель по отчёту включён
        (или состояние аксессуаров ещё неизвестно). Возвращает True, если байт отправлен."""
        with self._lock:
            st = self._state
            if not st.machine_state.startswith("Hold"):
                return False
            if st.accessories is not None and not any(c in st.accessories for c in "SC"):
                return False
        self._write(RT_SPINDLE_STOP)
        logger.info("Шпиндель остановлен (Spindle Stop override)")
        return True

    def abort_milling(self) -> None:
        """Прервать задание: feed hold -> остановка -> soft reset."""
        with self._lock:
            if not self._state.milling:
                return
        self._abort_event.set()

    
    
    
    
    # ========================================================
    # Public API: отдельные команды (выполняются в worker)
    # ========================================================

    def home(self) -> Future:
        """$H. Future -> машинные координаты после homing."""
        return self._submit("home", allow_alarm=True)

    def unlock(self) -> Future:
        """$X - снять Alarm без homing (позиция может быть неверной!)."""
        return self._submit("unlock", allow_alarm=True)

    def send_command(self, line: str) -> Future:
        """Произвольная строка G-code / $-команда. Future -> 'ok'."""
        line = line.strip()
        if not line or "\n" in line or "\r" in line:
            raise ValueError("Нужна ровно одна непустая строка")
        return self._submit("line", line=line)

    def soft_reset(self) -> None:
        """Ctrl-X. Во время движения приведёт к ALARM:3 (позиция потеряна)."""
        self._write(RT_SOFT_RESET)

    def probe_z_zero(
        self,
        distance: float = 10.0,
        feed: float = 50.0,
        plate_thickness: float = 0.0,
        retract: float = 2.0,
        set_zero: bool = True,
        coord_system: Optional[int] = None,
        second_pass_feed: Optional[float] = None,
        latch_distance: float = 1.0,
    ) -> Future:
        """
        Поиск нуля Z пробником (G38.2).

        distance         - максимальный ход ВНИЗ от текущей точки, мм
                           (независимо от G90/G91).
        feed             - подача поиска, мм/мин.
        plate_thickness  - толщина пластины: рабочий Z в точке касания
                           станет равен этому значению.
        retract          - отъезд вверх после касания, мм (0 - не отъезжать).
        set_zero         - записать результат в рабочую СК (G10 L20).
        coord_system     - 1..6 = G54..G59; None = активная СК.
        second_pass_feed - если задано: после первого касания отъехать на
                           latch_distance и коснуться ещё раз медленно.

        Future -> ProbeResult (машинные координаты точки касания).
        """
        if distance <= 0 or feed <= 0:
            raise ValueError("distance и feed должны быть > 0")
        if second_pass_feed is not None and second_pass_feed <= 0:
            raise ValueError("second_pass_feed должен быть > 0")
        return self._submit(
            "probe",
            distance=distance,
            feed=feed,
            plate_thickness=plate_thickness,
            retract=retract,
            set_zero=set_zero,
            coord_system=coord_system,
            second_pass_feed=second_pass_feed,
            latch_distance=latch_distance,
        )

    # ========================================================
    # TCP connection
    # ========================================================

    def _open_socket(self) -> None:
        logger.info("Подключение к ESP32 %s:%d", self.ip, self.port)
        try:
            sock = socket.create_connection(
                (self.ip, self.port), timeout=self.connect_timeout
            )
        except OSError as exc:
            raise CNCConnectionError(
                f"Не удалось подключиться к {self.ip}:{self.port}: {exc}"
            ) from exc

        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        # Таймаут только для sendall; чтение идёт через select().
        sock.settimeout(5.0)

        now = time.monotonic()
        with self._cond:
            self._sock = sock
            self._conn_gen += 1
            self._conn_time = now
            self._last_status_rx = now
            self._last_poll = 0.0
            self._rx_buf.clear()
            self._acks.clear()
            self._wco = None
            self._state.online = True
            self._state.machine_state = "Unknown"
            self._state.error = None
            self._cond.notify_all()

        logger.info("TCP-соединение с ESP32 установлено")

    def _drop_connection(
        self,
        sock: Optional[socket.socket],
        reason: str,
        log: bool = True,
    ) -> None:
        """Закрывает сокет. sock=None - закрыть текущий, какой бы ни был."""
        with self._cond:
            current = self._sock
            if current is None or (sock is not None and sock is not current):
                return
            self._sock = None
            self._conn_gen += 1
            self._state.online = False
            self._state.machine_state = "Unknown"
            if log:
                self._state.error = reason
            self._cond.notify_all()

        if log:
            logger.error("Соединение с ESP32 потеряно: %s", reason)

        for fn in (lambda: current.shutdown(socket.SHUT_RDWR), current.close):
            try:
                fn()
            except OSError:
                pass

    def _write(self, data: bytes) -> None:
        with self._send_lock:
            sock = self._sock
            if sock is None:
                raise CNCConnectionError("Нет TCP-соединения с ESP32")
            try:
                sock.sendall(data)
            except OSError as exc:
                self._drop_connection(sock, f"ошибка отправки: {exc}")
                raise CNCConnectionError(f"Ошибка отправки TCP: {exc}") from exc

    # ========================================================
    # IO thread
    # ========================================================

    def _io_loop(self) -> None:
        logger.info("IO thread запущен")
        delay = self.reconnect_initial_delay

        while not self._stop_event.is_set():
            with self._lock:
                sock = self._sock

            # ---------------- reconnect ----------------
            if sock is None:
                try:
                    self._open_socket()
                    delay = self.reconnect_initial_delay
                except CNCConnectionError as exc:
                    logger.warning("Reconnect не удался: %s", exc)
                    with self._lock:
                        self._state.error = str(exc)
                    self._stop_event.wait(delay)
                    delay = min(delay * 1.5, self.reconnect_max_delay)
                continue

            now = time.monotonic()

            # ---------------- опрос '?' ----------------
            if now - self._last_poll >= self.status_interval:
                self._last_poll = now
                try:
                    self._write(RT_STATUS)
                except CNCConnectionError:
                    continue

            with self._lock:
                silent = now - self._last_status_rx
            if silent > self.status_timeout:
                self._drop_connection(
                    sock, f"GRBL не отвечает на '?' {silent:.1f} c"
                )
                continue

            # ---------------- чтение ----------------
            try:
                readable, _, _ = select.select([sock], [], [], 0.05)
            except (OSError, ValueError) as exc:
                self._drop_connection(sock, f"select: {exc}")
                continue
            if not readable:
                continue

            try:
                data = sock.recv(4096)
            except OSError as exc:
                self._drop_connection(sock, f"ошибка чтения: {exc}")
                continue

            if not data:
                self._drop_connection(sock, "ESP32 закрыл соединение")
                continue

            self._feed(data)

        logger.info("IO thread завершён")

    def _feed(self, data: bytes) -> None:
        self._rx_buf.extend(data)
        while True:
            idx = self._rx_buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._rx_buf[:idx])
            del self._rx_buf[: idx + 1]
            line = raw.decode("ascii", errors="replace").strip()
            if line:
                self._handle_line(line)
        if len(self._rx_buf) > 4096:   # мусор без переводов строки
            self._rx_buf.clear()

    def _handle_line(self, line: str) -> None:
        logger.debug("<< %s", line)
        with self._cond:
            try:
                if line == "ok" or line.startswith("error:"):
                    self._acks.append(line)
                elif line.startswith("<") and line.endswith(">"):
                    self._parse_status(line)
                elif line.startswith("[PRB:"):
                    self._parse_probe(line)
                elif line.startswith("[GC:"):
                    self._parse_modal(line)
                elif line.startswith("ALARM:"):
                    code = int(line[6:])
                    self._alarm_seq += 1
                    self._state.alarm = code
                    self._state.error = str(CNCAlarmError(code))
                    logger.error("%s", self._state.error)
                elif line.startswith("Grbl "):
                    self._reset_seq += 1
                    self._acks.clear()
                    logger.warning("GRBL перезагрузился: %s", line)
                elif line.startswith("[MSG:"):
                    logger.info("GRBL %s", line)
                else:
                    logger.debug("GRBL прочее: %s", line)
            except ValueError:
                logger.warning("Не удалось разобрать строку GRBL: %r", line)
            self._cond.notify_all()

    # ---------------- парсеры (вызываются под _lock) ----------------

    @staticmethod
    def _vec(text: str) -> Vec3:
        parts = [float(v) for v in text.split(",")]
        while len(parts) < 3:
            parts.append(0.0)
        return (parts[0], parts[1], parts[2])

    def _parse_status(self, line: str) -> None:
        # <Idle|MPos:135.000,0.000,-4.516|FS:0,0|WCO:-10.000,0.000,-10.503>
        parts = line[1:-1].split("|")
        fields = {}
        for part in parts[1:]:
            key, _, value = part.partition(":")
            fields[key] = value

        st = self._state
        st.machine_state = parts[0]

        if "WCO" in fields:
            self._wco = self._vec(fields["WCO"])

        mpos = self._vec(fields["MPos"]) if "MPos" in fields else None
        wpos = self._vec(fields["WPos"]) if "WPos" in fields else None
        wco = self._wco

        if mpos is not None and wpos is None and wco is not None:
            wpos = (mpos[0] - wco[0], mpos[1] - wco[1], mpos[2] - wco[2])
        elif wpos is not None and mpos is None and wco is not None:
            mpos = (wpos[0] + wco[0], wpos[1] + wco[1], wpos[2] + wco[2])

        st.mpos = mpos
        st.wpos = wpos
        st.wco = wco
        if wpos is not None:
            st.x, st.y, st.z = wpos
        else:
            st.x = st.y = st.z = None

        if "FS" in fields:
            fs = fields["FS"].split(",")
            st.feed_rate = float(fs[0])
            st.spindle_rpm = float(fs[1]) if len(fs) > 1 else 0.0
        elif "F" in fields:
            st.feed_rate = float(fields["F"])

        if "Ov" in fields or "A" in fields:
            # A: приходит вместе с Ov; отсутствие A при Ov — аксессуары выключены
            st.accessories = fields.get("A", "")

        st.pins = fields.get("Pn", "")
        st.probe_pin = "P" in st.pins

        if "Bf" in fields:
            bf = fields["Bf"].split(",")
            st.planner_free = int(bf[0])
            st.rx_free = int(bf[1]) if len(bf) > 1 else None

        if not st.machine_state.startswith("Alarm"):
            st.alarm = None

        st.status_time = time.monotonic()
        self._last_status_rx = st.status_time

    def _parse_probe(self, line: str) -> None:
        # [PRB:135.000,0.000,-4.461:1]
        coords, _, flag = line[5:-1].rpartition(":")
        x, y, z = self._vec(coords)
        self._state.last_probe = ProbeResult(
            x=x, y=y, z=z, success=(flag.strip() == "1"),
            timestamp=time.monotonic(),
        )
        self._probe_seq += 1
        logger.info("Probe: %s", self._state.last_probe)

    def _parse_modal(self, line: str) -> None:
        # [GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]
        tokens = line[4:-1].split()
        st = self._state
        for tok in tokens:
            if tok == "G90":
                st.absolute_mode = True
            elif tok == "G91":
                st.absolute_mode = False
            elif re.fullmatch(r"G5[4-9]", tok):
                st.coord_system = tok
            elif tok in ("M3", "M4"):
                st.spindle_on = True
                st.spindle_direction = int(tok[1])
            elif tok == "M5":
                st.spindle_on = False
                st.spindle_direction = 0

    # ========================================================
    # Ожидания
    # ========================================================

    def _wait_status(
        self,
        predicate: Callable[[CNCState], bool],
        timeout: float,
        what: str,
        since: Optional[float] = None,
    ) -> CNCState:
        """Ждёт отчёт '?', полученный ПОСЛЕ since, для которого predicate()."""
        t0 = time.monotonic() if since is None else since
        deadline = time.monotonic() + timeout
        with self._cond:
            gen = self._conn_gen
            while True:
                st = self._state
                if st.status_time > t0 and predicate(st):
                    return replace(st)
                if self._stop_event.is_set():
                    raise CNCAbortedError("Контроллер остановлен")
                if self._conn_gen != gen or self._sock is None:
                    raise CNCConnectionError(f"Связь потеряна при ожидании {what}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CNCTimeoutError(f"Не дождались {what} за {timeout:.1f} c")
                self._cond.wait(min(remaining, 0.1))

    def _wait_idle(self, timeout: float) -> CNCState:
        def pred(s: CNCState) -> bool:
            if s.machine_state.startswith("Alarm"):
                raise CNCAlarmError(s.alarm)
            return s.machine_state == "Idle"
        return self._wait_status(pred, timeout, "состояния Idle")

    def _wait_link_ready(self, timeout: float = 5.0) -> None:
        """Ждёт, пока есть сокет и пришёл хотя бы один статус по нему."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not (self._sock is not None
                       and self._state.status_time > self._conn_time):
                if self._stop_event.is_set():
                    raise CNCAbortedError("Контроллер остановлен")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CNCConnectionError("Нет связи с GRBL")
                self._cond.wait(min(remaining, 0.1))

    # ========================================================
    # Отправка строки G-code и ожидание ok
    # ========================================================

    def _send_line(
        self,
        line: str,
        *,
        allow_alarm: bool = False,
        extra_timeout: float = 0.0,
    ) -> str:
        """
        Отправляет одну строку и ждёт ok / error:N.

        Возвращает "ok" или (только при allow_alarm=True) "ALARM".
        error:N -> CNCGrblError. Повторной отправки при обрыве НЕТ
        (см. комментарий в _run_job).

        ВАЖНО: ok в GRBL означает "строка разобрана и поставлена в буфер
        планировщика", а НЕ "движение выполнено". Чтобы дождаться
        окончания движения - _wait_idle() или строка G4 P0.01.
        """
        line = line.strip()
        if not line:
            return "ok"

        self._wait_link_ready()

        with self._cond:
            if self._acks:
                logger.warning("Лишние ответы GRBL отброшены: %s", list(self._acks))
                self._acks.clear()
            gen = self._conn_gen
            reset0 = self._reset_seq
            alarm0 = self._alarm_seq

        logger.debug(">> %s", line)
        self._write((line + "\n").encode("ascii", errors="replace"))

        last_progress = time.monotonic()
        alarm_deadline: Optional[float] = None

        with self._cond:
            while True:
                if self._acks:
                    resp = self._acks.popleft()
                    break

                now = time.monotonic()

                if self._stop_event.is_set():
                    raise CNCAbortedError("Контроллер остановлен")
                if self._abort_event.is_set():
                    raise CNCAbortedError("Задание прервано")
                if self._conn_gen != gen:
                    raise CNCConnectionError(
                        f"Связь потеряна, ответ на {line!r} не получен"
                    )
                if self._reset_seq != reset0:
                    raise CNCResetError(f"GRBL перезагрузился во время {line!r}")
                if self._alarm_seq != alarm0:
                    if not allow_alarm:
                        raise CNCAlarmError(self._state.alarm)
                    # После ALARM при пробинге ok может прийти, а может и нет.
                    if alarm_deadline is None:
                        alarm_deadline = now + 1.0
                    elif now > alarm_deadline:
                        return "ALARM"

                if self._state.machine_state.startswith(_BUSY_STATES):
                    last_progress = now
                elif now - last_progress > self.idle_ack_timeout + extra_timeout:
                    raise CNCTimeoutError(
                        f"Нет ответа на {line!r}, станок в "
                        f"{self._state.machine_state}"
                    )

                self._cond.wait(0.1)

            alarm_seen = self._alarm_seq != alarm0

        if resp.startswith("error:"):
            try:
                code = int(resp[6:])
            except ValueError:
                code = -1
            raise CNCGrblError(code, line)

        if alarm_seen:
            if allow_alarm:
                return "ALARM"
            raise CNCAlarmError(self._state.alarm)

        return "ok"

    # ========================================================
    # Worker
    # ========================================================

    def _worker_loop(self) -> None:
        logger.info("Worker thread запущен")

        while not self._stop_event.is_set():
            with self._lock:
                milling = self._state.milling

            if milling:
                self._run_job()
                continue

            try:
                task = self._tasks.get(timeout=0.1)
            except queue.Empty:
                continue

            if not task.future.set_running_or_notify_cancel():
                continue

            try:
                result = self._execute_task(task)
            except BaseException as exc:          # noqa: BLE001
                logger.error("Команда %s не выполнена: %s", task.kind, exc)
                with self._lock:
                    self._state.error = str(exc)
                task.future.set_exception(exc)
            else:
                task.future.set_result(result)

        logger.info("Worker thread завершён")

    def _submit(self, kind: str, allow_alarm: bool = False, **payload) -> Future:
        self._check_ready(allow_alarm=allow_alarm)
        task = _Task(kind=kind, payload=payload)
        self._tasks.put(task)
        logger.info("Команда %s добавлена в очередь %s", kind, payload or "")
        return task.future

    def _check_ready(self, allow_alarm: bool) -> None:
        if not self.is_running():
            raise CNCError("CNCController не запущен")
        with self._lock:
            if self._state.milling:
                raise CNCBusyError("Сейчас идёт фрезеровка")
            if not self._state.online:
                raise CNCConnectionError("Фрезер не подключен")
            if not allow_alarm and self._state.machine_state.startswith("Alarm"):
                raise CNCAlarmError(
                    self._state.alarm, "Нужен home() ($H) или unlock() ($X)"
                )

    def _execute_task(self, task: _Task):
        kind = task.kind
        p = task.payload

        if kind == "line":
            return self._send_line(p["line"], extra_timeout=_dwell_seconds(p["line"]))

        if kind == "unlock":
            return self._send_line("$X")

        if kind == "home":
            # ok на $H приходит ПОСЛЕ завершения цикла homing.
            self._send_line("$H")
            st = self._wait_idle(10.0)
            logger.info("Homing завершён: MPos=%s WPos=%s", st.mpos, st.wpos)
            return st.mpos

        if kind == "probe":
            return self._probe_z(**p)

        raise CNCError(f"Неизвестная задача {kind}")

    # ========================================================
    # Milling
    # ========================================================

    def _run_job(self) -> None:
        """
        Стримит файл по протоколу send-response.

        При обрыве связи задание ПРЕРЫВАЕТСЯ, а не продолжается с той же
        строки: если строка дошла до GRBL, а потерялся только ok, повторная
        отправка выполнит её дважды (в G91 - лишнее смещение, врезание).
        GRBL при этом доработает уже принятые в буфер (до ~15) перемещения.
        """
        error: Optional[str] = None
        try:
            while True:
                if self._stop_event.is_set():
                    raise CNCAbortedError("Контроллер остановлен")
                if self._abort_event.is_set():
                    raise CNCAbortedError("Фрезеровка прервана")
                if self._pause_event.is_set():
                    time.sleep(0.05)
                    continue
                if self._gcode_position >= len(self._gcode_lines):
                    break

                line = self._gcode_lines[self._gcode_position]
                self._send_line(line, extra_timeout=_dwell_seconds(line))

                self._gcode_position += 1
                with self._lock:
                    self._state.current_line = self._gcode_position
                    self._state.progress = (
                        self._gcode_position / max(1, self._state.total_lines) * 100.0
                    )

            # Все строки приняты GRBL, но ещё не выполнены. G4 синхронизирует
            # буфер: ok на неё придёт только после окончания всех движений.
            self._send_line("G4 P0.01")

        except (CNCAbortedError, CNCGrblError, CNCTimeoutError) as exc:
            error = str(exc)
            self._safe_stop()
        except (CNCAlarmError, CNCResetError) as exc:
            error = str(exc)        # GRBL уже остановился сам
        except CNCConnectionError as exc:
            error = (f"{exc}. Задание прервано; GRBL доработает уже "
                     f"принятые в буфер команды и остановится")
        except Exception as exc:    # noqa: BLE001
            logger.exception("Неожиданная ошибка фрезеровки")
            error = str(exc)
            self._safe_stop()

        self._finish_job(error)

    def _finish_job(self, error: Optional[str]) -> None:
        with self._lock:
            self._state.milling = False
            self._state.paused = False
            if error is not None:
                self._state.error = error
            else:
                self._state.progress = 100.0
        self._pause_event.clear()
        self._abort_event.clear()
        self._gcode_lines = []
        self._gcode_position = 0

        if error is None:
            logger.info("Фрезеровка завершена")
        else:
            logger.error("Фрезеровка завершена с ошибкой: %s", error)

    def _safe_stop(self) -> None:
        """
        Остановка без потери позиции: '!' -> ждём Hold:0 -> Ctrl-X.
        Reset сразу во время движения дал бы ALARM:3 и потерю позиции.
        """
        logger.warning("Безопасная остановка: feed hold + reset")
        try:
            self._write(RT_FEED_HOLD)
            try:
                self._wait_status(
                    lambda s: s.machine_state in ("Hold:0", "Idle")
                    or s.machine_state.startswith("Alarm"),
                    30.0, "остановки (Hold:0)",
                )
            except CNCTimeoutError as exc:
                logger.warning("%s - всё равно делаем reset", exc)

            with self._lock:
                reset0 = self._reset_seq
            self._write(RT_SOFT_RESET)

            deadline = time.monotonic() + 3.0
            with self._cond:
                while self._reset_seq == reset0 and time.monotonic() < deadline:
                    self._cond.wait(0.1)
            self._pause_event.clear()
            with self._lock:
                self._state.paused = False
        except CNCError as exc:
            logger.error("Безопасная остановка не удалась: %s", exc)

    # ========================================================
    # Z probe
    # ========================================================

    def _probe_z(
        self,
        distance: float,
        feed: float,
        plate_thickness: float,
        retract: float,
        set_zero: bool,
        coord_system: Optional[int],
        second_pass_feed: Optional[float],
        latch_distance: float,
    ) -> ProbeResult:
        st = self.refresh_status(1.0)
        if st.machine_state.startswith("Alarm"):
            raise CNCAlarmError(st.alarm, "Перед пробингом нужен $H или $X")
        if st.machine_state != "Idle":
            raise CNCBusyError(f"Станок не в Idle: {st.machine_state}")
        if st.probe_pin:
            raise CNCProbeError(
                "Пробник уже показывает контакт (Pn:P). Проверьте провод/клипсу "
                "и инверсию $6."
            )

        # Узнаём G90/G91 и активную СК, чтобы ничего в них не менять.
        self._send_line("$G")
        with self._lock:
            absolute = self._state.absolute_mode
            active_cs = self._state.coord_system

        result, stop_z = self._probe_move(distance, feed, absolute)

        if second_pass_feed is not None:
            self._move_z_relative(latch_distance, absolute)
            result, stop_z = self._probe_move(
                latch_distance * 2.0, second_pass_feed, absolute
            )

        if set_zero:
            cs = coord_system or (int(active_cs[2]) - 3)   # G54 -> 1 ... G59 -> 6
            # После G38.2 станок стоит НИЖЕ точки срабатывания на величину
            # тормозного пути (в вашем логе: -4.516 против -4.461 = 0.055 мм).
            # G10 L20 задаёт значение для ТЕКУЩЕЙ позиции, поэтому учитываем
            # перебег: текущий Z = толщина пластины + (стоп - касание).
            overshoot = stop_z - result.z
            value = plate_thickness + overshoot
            self._send_line(f"G10 L20 P{cs} Z{value:.4f}")
            logger.info(
                "Z0 установлен (G%d): касание MPos Z=%.3f, перебег %.3f мм",
                53 + cs, result.z, overshoot,
            )

        if retract > 0:
            self._move_z_relative(retract, absolute)

        return result

    def _probe_move(
        self, distance: float, feed: float, absolute: bool
    ) -> Tuple[ProbeResult, float]:
        """Один G38.2 вниз на distance. Возвращает (результат, MPos Z стопа)."""
        with self._lock:
            probe0 = self._probe_seq

        if absolute:
            # В G90 "G38.2 Z-10" - это абсолютная цель Z=-10 в рабочей СК,
            # а не "10 мм вниз". Считаем цель от текущей позиции.
            st = self._wait_status(lambda s: s.wpos is not None, 5.0,
                                   "рабочих координат (WCO)")
            cmd = f"G38.2 Z{st.wpos[2] - distance:.4f} F{feed:g}"
        else:
            cmd = f"G38.2 Z{-distance:.4f} F{feed:g}"

        resp = self._send_line(
            cmd, allow_alarm=True, extra_timeout=distance / feed * 60.0
        )

        # [PRB:...] приходит до ok, но подстрахуемся.
        deadline = time.monotonic() + 2.0
        with self._cond:
            while self._probe_seq == probe0 and time.monotonic() < deadline:
                self._cond.wait(0.1)
            got = self._probe_seq != probe0
            result = self._state.last_probe
            alarm = self._state.alarm

        if resp == "ALARM" or not got or result is None or not result.success:
            try:
                self.refresh_status(1.0)   # чтобы get_state() сразу показал Alarm
            except CNCError:
                pass
            reason = GRBL_ALARMS.get(alarm, "нет [PRB] или PRB:...:0") if alarm \
                else "нет [PRB] или PRB:...:0"
            raise CNCProbeError(
                f"Пробинг не удался ({cmd}): {reason}. "
                f"GRBL в Alarm - для продолжения unlock() или home()."
            )

        st = self._wait_status(
            lambda s: s.machine_state == "Idle" and s.mpos is not None,
            5.0, "остановки после пробинга",
        )
        return result, st.mpos[2]

    def _move_z_relative(self, dz: float, absolute: bool) -> None:
        """Переместиться по Z на dz, не меняя G90/G91."""
        if absolute:
            # Свежий отчёт: после G10 GRBL сразу присылает новый WCO.
            st = self._wait_status(lambda s: s.wpos is not None, 5.0,
                                   "рабочих координат")
            self._send_line(f"G0 Z{st.wpos[2] + dz:.4f}")
        else:
            self._send_line(f"G0 Z{dz:.4f}")
        self._wait_idle(30.0)

    # ========================================================
    # G-code file
    # ========================================================

    @staticmethod
    def _remove_comments(line: str) -> str:
        line = re.sub(r"\([^)]*\)", "", line)
        line = line.split(";", 1)[0]
        return line.strip()

    def _load_gcode(self, path: Path) -> list[str]:
        result: list[str] = []
        with path.open("r", encoding="utf-8", errors="replace") as file:
            for number, raw in enumerate(file, start=1):
                line = self._remove_comments(raw)
                if not line or line == "%":
                    continue
                if not line.isascii():
                    raise ValueError(f"Строка {number}: не-ASCII символы: {raw!r}")
                compact = line.replace(" ", "").replace("\t", "")
                if len(compact) > GRBL_MAX_LINE:
                    raise ValueError(
                        f"Строка {number}: {len(compact)} символов, GRBL "
                        f"принимает максимум {GRBL_MAX_LINE}"
                    )
                result.append(line)
        return result

    # ========================================================
    # Context manager
    # ========================================================

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def _dwell_seconds(line: str) -> float:
    """Длительность G4 P.. в строке (чтобы не словить таймаут на паузе)."""
    upper = line.upper()
    if re.search(r"G0*4(?![\d.])", upper):
        m = re.search(r"P\s*([\d.]+)", upper)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return 0.0


# ============================================================
# Example
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GRBL via ESP32 bridge")
    parser.add_argument("ip")
    parser.add_argument("--port", type=int, default=8881)
    parser.add_argument("--file", help="G-code файл для фрезеровки")
    parser.add_argument("--probe", action="store_true", help="найти Z0 пробником")
    parser.add_argument("--plate", type=float, default=0.0, help="толщина пластины")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
    )

    def fmt(v: Optional[Vec3]) -> str:
        return "?" if v is None else f"{v[0]:8.3f} {v[1]:8.3f} {v[2]:8.3f}"

    cnc = CNCController(ip=args.ip, port=args.port)
    try:
        cnc.start()

        if args.probe:
            res = cnc.probe_z_zero(
                distance=10, feed=50, plate_thickness=args.plate,
                second_pass_feed=20,
            ).result(timeout=120)
            print(f"Касание (MPos): Z={res.z:.3f}")

        if args.file:
            cnc.start_milling(args.file)

        while True:
            s = cnc.get_state()
            print(
                f"\r{s.machine_state:8} | WPos {fmt(s.wpos)} | MPos {fmt(s.mpos)} "
                f"| F={s.feed_rate:.0f} S={s.spindle_rpm:.0f} "
                f"| {s.progress:5.1f}%",
                end="", flush=True,
            )
            if args.file and not s.milling:
                print("\nГотово" if s.error is None else f"\nОшибка: {s.error}")
                break
            time.sleep(0.25)

    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        cnc.stop()

# dome_simulator/time_control.py
"""
Управление скоростью симуляционного времени.

Идея разделения:
- Реальный период цикла потока (tick_interval, сек) держится постоянным
  и стабилизируется компенсацией дрейфа в SimulatorThread.
- time_scale влияет только на то, какой dt (симуляционное время)
  получают узлы за один реальный тик. time_scale=0 -> dt=0 (пауза),
  поток при этом продолжает крутиться и спать положенный интервал.
"""
from __future__ import annotations

import threading


class TimeController:
    def __init__(self, tick_interval: float = 4.0, time_scale: float = 1.0) -> None:
        if tick_interval <= 0:
            raise ValueError("tick_interval должен быть > 0")
        if time_scale < 0:
            raise ValueError("time_scale не может быть отрицательным")

        self._lock = threading.RLock()
        self._tick_interval = tick_interval
        self._time_scale = time_scale
        self._paused = False
        self._sim_time: float = 0.0  # накопленное симуляционное время, сек

    # ---------- управление ----------

    def set_time_scale(self, scale: float) -> None:
        if scale < 0:
            raise ValueError("time_scale не может быть отрицательным")
        with self._lock:
            self._time_scale = scale

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def set_tick_interval(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("tick_interval должен быть > 0")
        with self._lock:
            self._tick_interval = seconds

    # ---------- чтение ----------

    @property
    def tick_interval(self) -> float:
        with self._lock:
            return self._tick_interval

    @property
    def time_scale(self) -> float:
        with self._lock:
            return self._time_scale

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def sim_time(self) -> float:
        with self._lock:
            return self._sim_time

    def compute_dt(self) -> float:
        """
        Вызывается SimulatorThread ровно раз за тик.
        Возвращает симуляционный dt (сек) для передачи в node.tick(dt, ...).
        При паузе (флаг или time_scale=0) возвращает 0.0, но не блокирует поток.
        """
        with self._lock:
            if self._paused or self._time_scale == 0.0:
                return 0.0
            dt = self._tick_interval * self._time_scale
            self._sim_time += dt
            return dt
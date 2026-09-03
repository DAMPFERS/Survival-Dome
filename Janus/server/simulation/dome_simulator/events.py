# dome_simulator/events.py
"""
Шина событий симулятора.

Событие ставится в очередь методом publish() и рассылается подписчикам
только при явном вызове dispatch_pending() — этот вызов делает
SimulatorThread один раз в конце каждого тика. Такой отложенный
диспатч даёт предсказуемый порядок обработки и не даёт подписчикам
видеть "недособранное" состояние тика.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Optional
from collections import deque

logger = logging.getLogger("dome_simulator.events")


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Event:
    """Неизменяемое событие. frozen+slots — дёшево создавать пачками за тик."""
    type: str
    source: str                       # node_id или имя подсистемы ("dependency_engine", "scenario:power_loss")
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    severity: Severity = Severity.INFO

    def __repr__(self) -> str:  # компактный лог
        return f"Event({self.type!r} <- {self.source}, {self.severity.value})"


EventCallback = Callable[[Event], None]


class EventBus:
    """
    Потокобезопасная шина "publish -> (очередь) -> dispatch_pending -> subscribers".

    - publish() дешёвый и неблокирующий (кладёт в queue.Queue).
    - subscribe(event_type, cb) / subscribe_all(cb) регистрируют обработчики.
    - dispatch_pending() достаёт всё накопленное и рассылает синхронно,
      вызывается ТОЛЬКО из цикла SimulatorThread.
    - Хранится ограниченная история (deque) для отладки/API (get_recent).
    """

    def __init__(self, history_size: int = 500) -> None:
        self._queue: "queue.Queue[Event]" = queue.Queue()
        self._subscribers: dict[str, list[EventCallback]] = {}
        self._subscribers_all: list[EventCallback] = []
        self._lock = threading.RLock()  # защищает только структуры подписчиков/историю
        self._history: Deque[Event] = deque(maxlen=history_size)

    def publish(self, event: Event) -> None:
        """Кладёт событие в очередь. Может вызываться из любого потока."""
        self._queue.put(event)

    def subscribe(self, event_type: str, callback: EventCallback) -> None:
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

    def subscribe_all(self, callback: EventCallback) -> None:
        with self._lock:
            self._subscribers_all.append(callback)

    def unsubscribe(self, event_type: str, callback: EventCallback) -> None:
        with self._lock:
            handlers = self._subscribers.get(event_type)
            if handlers and callback in handlers:
                handlers.remove(callback)

    def dispatch_pending(self) -> int:
        """
        Забирает все накопленные события и рассылает подписчикам.
        Возвращает количество обработанных событий.
        Вызывается из SimulatorThread в конце тика.
        """
        processed = 0
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            self._dispatch_one(event)
            processed += 1
        return processed

    def _dispatch_one(self, event: Event) -> None:
        with self._lock:
            self._history.append(event)
            handlers = list(self._subscribers.get(event.type, ()))
            handlers_all = list(self._subscribers_all)

        # Колбэки вызываем ВНЕ лока подписчиков, чтобы подписчик мог
        # изнутри вызвать subscribe()/publish() без дедлока.
        for cb in (*handlers, *handlers_all):
            try:
                cb(event)
            except Exception:  # обработчик не должен уронить весь тик
                logger.exception("Ошибка в обработчике события %s", event)

    def get_recent(self, n: int = 50, event_type: Optional[str] = None) -> list[Event]:
        with self._lock:
            items = list(self._history)
        if event_type is not None:
            items = [e for e in items if e.type == event_type]
        return items[-n:]

    def pending_count(self) -> int:
        return self._queue.qsize()
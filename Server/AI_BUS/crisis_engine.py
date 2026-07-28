"""
Генератор нештатных событий (кризисов): фоновый поток, который через случайные
интервалы времени добавляет в общий чат событие-триггер (is_event=True).

ВАЖНО (ограничение прототипа): событие носит информационный характер и НЕ
подделывает реальные показания телеметрии — агенты, среагировав, пойдут
проверять РЕАЛЬНОЕ состояние через свои инструменты. Если хотите, чтобы
кризис физически менял показания (например, реально роняя генерацию солнца),
нужен либо mock-режим на стороне client.py/сервера, либо метод вроде
`inject_fault(...)` на сервере телеметрии — это уже выходит за рамки
приложенного примера и должно быть согласовано с тем, кто пишет сервер.
"""

import logging
import random
import threading
from typing import List, Optional

from chat_bus import SharedChatLog

logger = logging.getLogger(__name__)

CRISIS_SCENARIOS: List[str] = [
    "Резкий скачок потребления энергии — одна из линий нагрузки внезапно "
    "начала потреблять аномально много. Проверьте состояние каналов и батарею.",

    "Уровень CO2 внутри купола начал быстро расти. Возможна неисправность "
    "вентиляции. Требуется проверка климата и незамедлительные меры.",

    "Пропала связь с внешним миром — сигнал упал до минимума. Проверьте "
    "каналы связи и готовность аварийного маяка.",

    "Резкое падение генерации солнечных панелей при полном отсутствии "
    "внешних причин (не ночь, нет облачности по прогнозу). Возможна "
    "неисправность панелей.",

    "Батарея разряжается быстрее ожидаемого при номинальной нагрузке. "
    "Возможна деградация ёмкости аккумулятора.",

    "Резкий скачок температуры внутри купола при нормальной внешней "
    "температуре. Проверьте климат-контроль.",

    "Фрезерный станок сообщает об аварийной остановке во время задания. "
    "Требуется проверка станка и, при необходимости, перезапуск.",
]


class CrisisEngine:
    def __init__(self, bus: SharedChatLog, min_interval_sec: float = 90.0,
                 max_interval_sec: float = 300.0, scenarios: Optional[List[str]] = None):
        self._bus = bus
        self._min = min_interval_sec
        self._max = max_interval_sec
        self._scenarios = scenarios or CRISIS_SCENARIOS
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("CrisisEngine запущен (интервал %.0f-%.0f сек)", self._min, self._max)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            wait = random.uniform(self._min, self._max)
            if self._stop_event.wait(wait):
                break
            scenario = random.choice(self._scenarios)
            logger.warning("КРИЗИС: %s", scenario)
            self._bus.append(speaker="СОБЫТИЕ", content=scenario, is_event=True)

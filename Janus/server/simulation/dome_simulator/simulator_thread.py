# dome_simulator/simulator_thread.py
"""
Главный цикл симулятора в отдельном потоке (п. "Отдельный поток" ТЗ).

Компенсация дрейфа: вместо time.sleep(tick_interval) на каждой итерации
считаем АБСОЛЮТНУЮ точку следующего тика (next_tick_time += interval) и
спим до неё. Так суммарная ошибка не накапливается даже при небольших
задержках выполнения самого тика — средний период стремится к tick_interval
именно как требует ТЗ ("средний период оставался равным tick_interval").
"""
from __future__ import annotations

import logging
import threading
import time

from .dependency_engine import DependencyEngine
from .events import EventBus
from .nodes.base import NodeRegistry
from .scenario_engine import ScenarioEngine
from .store import StateStore
from .time_control import TimeController

logger = logging.getLogger("dome_simulator.thread")


class SimulatorThread(threading.Thread):
    def __init__(
        self,
        store: StateStore,
        event_bus: EventBus,
        registry: NodeRegistry,
        dependency_engine: DependencyEngine,
        scenario_engine: ScenarioEngine,
        time_controller: TimeController,
    ) -> None:
        super().__init__(name="DomeSimulatorThread", daemon=True)
        self._store = store
        self._event_bus = event_bus
        self._registry = registry
        self._dependency_engine = dependency_engine
        self._scenario_engine = scenario_engine
        self._time_controller = time_controller
        self._stop_event = threading.Event()
        self.tick_count = 0

    def stop(self) -> None:
        """Сигнал на завершение потока. join() делает вызывающий код (Simulator.stop())."""
        self._stop_event.set()

    def run(self) -> None:
        logger.info("SimulatorThread запущен, tick_interval=%.2fs", self._time_controller.tick_interval)
        next_tick_time = time.monotonic()

        while not self._stop_event.is_set():
            try:
                self._run_single_tick()
            except Exception:
                # Тик не должен ронять весь поток симулятора — логируем и живём дальше.
                logger.exception("Необработанная ошибка в тике #%d", self.tick_count)

            self.tick_count += 1
            interval = self._time_controller.tick_interval
            next_tick_time += interval
            sleep_time = next_tick_time - time.monotonic()

            if sleep_time > 0:
                # wait() вместо sleep() — позволяет stop() прервать ожидание мгновенно
                self._stop_event.wait(timeout=sleep_time)
            else:
                # Тик(и) выполнялись дольше interval — не пытаемся "догнать" пачкой
                # тиков подряд (это дало бы скачок dt-подобной нагрузки), а просто
                # сбрасываем базовую точку отсчёта.
                logger.warning("Тик #%d выполнялся дольше tick_interval (%.3fs просрочки)",
                                self.tick_count, -sleep_time)
                next_tick_time = time.monotonic()

        logger.info("SimulatorThread остановлен после %d тиков", self.tick_count)

    def _run_single_tick(self) -> None:
        dt = self._time_controller.compute_dt()

        # 1-2. Тик всех узлов, централизованная публикация их событий
        for node in self._registry.all_nodes():
            try:
                events = node.tick(dt, self._store, self._event_bus)
            except Exception:
                logger.exception("Ошибка в tick() узла %r", node.node_id)
                continue
            for event in events:
                self._event_bus.publish(event)

        # 3. Сценарии/кризисы
        self._scenario_engine.tick(self._store, self._event_bus, dt)

        # 4. Зависимости между узлами
        self._dependency_engine.run(self._store, self._event_bus, dt)

        # 5. Единственная точка рассылки событий подписчикам за тик
        dispatched = self._event_bus.dispatch_pending()
        if dispatched:
            logger.debug("Тик #%d: dt=%.3f, разослано событий: %d", self.tick_count, dt, dispatched)
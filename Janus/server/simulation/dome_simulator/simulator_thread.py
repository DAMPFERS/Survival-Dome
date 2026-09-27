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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .simulator import Simulator

logger = logging.getLogger("dome_simulator.thread")


def run_tick(dt: float, sim: "Simulator") -> int:
    """
    Один тик симулятора. Используется SimulatorThread и Simulator.step()
    (синхронный шаг для тестов/отладки). Возвращает число разосланных событий.

    Шаги 0–5 выполняются под блокировкой хранилища: внешние читатели видят
    состояние только между тиками, а управляющие команды не теряются.
    Рассылка событий — вне блокировки, чтобы подписчики могли свободно
    обращаться к симулятору из других потоков.
    """
    store, event_bus, ctx = sim.store, sim.event_bus, sim.ctx
    with store.transaction():
        # 0. Окружение: погода (игровое время), воздух купола (время устройств)
        sim.environment.step(dt)

        # 1-2. Тик всех узлов, централизованная публикация их событий
        for node in sim.registry.all_nodes():
            try:
                events = node.tick(dt, store, event_bus)
            except Exception:
                logger.exception("Ошибка в tick() узла %r", node.node_id)
                continue
            for event in events:
                event_bus.publish(event)

        # 3. Сценарии/кризисы
        sim.scenario_engine.tick(ctx, dt)

        # 4. Зависимости между узлами и автоматика купола
        sim.dependency_engine.run(store, event_bus, dt)

        # 5. Смена: учёт метрик (если идёт)
        if sim.shift is not None and dt > 0:
            try:
                sim.shift.tick(dt)
            except Exception:
                logger.exception("Ошибка учёта метрик смены")

        # 6. Публикация телеметрии, которую видит агент
        if dt > 0:
            sim.telemetry.publish()

    # 7. Единственная точка рассылки событий подписчикам за тик
    return event_bus.dispatch_pending()


class SimulatorThread(threading.Thread):
    def __init__(self, sim: "Simulator") -> None:
        super().__init__(name="DomeSimulatorThread", daemon=True)
        self._sim = sim
        self._time_controller = sim.time_controller
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
        dispatched = run_tick(dt, self._sim)
        if dispatched:
            logger.debug("Тик #%d: dt=%.3f, разослано событий: %d", self.tick_count, dt, dispatched)

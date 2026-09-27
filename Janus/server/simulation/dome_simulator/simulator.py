# dome_simulator/simulator.py
"""
Simulator — публичный фасад (п.4.1 ТЗ). Единственная точка входа для
API Gateway / ИИ-агентов участников хакатона. Собирает все компоненты
и предоставляет требуемый интерфейс:

    start(), stop(), pause(), resume()
    set_time_scale(scale)
    get(node_id, param), get_node(node_id), get_all(), snapshot()
    set(node_id, param, value), update(node_id, **kwargs)
    trigger_crisis(name, params=None), stop_crisis(name)

Плюс то, что необходимо практически, но не расписано дословно в 4.1:
регистрация узлов, подписка на события, управляющие команды (control),
персистентность (dump/load snapshot).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .dependency_engine import DependencyEngine
from .environment import Environment
from .events import Event, EventBus, EventCallback
from .nodes.base import BaseNode, NodeRegistry, create_node
from .nodes.sandbox import SandboxNode
from .scenario_engine import ScenarioEngine
from .store import StateStore
from .time_control import TimeController

logger = logging.getLogger("dome_simulator.simulator")


class Simulator:
    """Фасад. Владеет жизненным циклом всех подсистем купола."""

    def __init__(self, tick_interval: float = 4.0, time_scale: float = 1.0,
                 day_length_s: float = 1440.0, seed: Optional[int] = None) -> None:
        self.store = StateStore()
        self.event_bus = EventBus()
        self.environment = Environment(day_length_s=day_length_s, seed=seed)
        self.registry = NodeRegistry(self.store, self.environment)
        self.dependency_engine = DependencyEngine()
        self.scenario_engine = ScenarioEngine()
        self.time_controller = TimeController(tick_interval=tick_interval, time_scale=time_scale)

        self._thread: Optional[Any] = None  # SimulatorThread, создаётся в start()

    # ------------------------------------------------------------------
    # Жизненный цикл (п.4.1)
    # ------------------------------------------------------------------

    def start(self) -> None:
        from .simulator_thread import SimulatorThread  # локальный импорт: избегаем цикла модулей

        if self._thread is not None and self._thread.is_alive():
            logger.warning("Simulator уже запущен")
            return
        self._thread = SimulatorThread(
            store=self.store,
            event_bus=self.event_bus,
            registry=self.registry,
            dependency_engine=self.dependency_engine,
            scenario_engine=self.scenario_engine,
            time_controller=self.time_controller,
            environment=self.environment,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._thread.stop()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.error("SimulatorThread не завершился за %.1fs", timeout)
        self._thread = None

    def pause(self) -> None:
        self.time_controller.pause()

    def resume(self) -> None:
        self.time_controller.resume()

    def set_time_scale(self, scale: float) -> None:
        self.time_controller.set_time_scale(scale)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def step(self, dt: Optional[float] = None) -> int:
        """
        Синхронно выполняет один тик в вызывающем потоке (тесты, отладка,
        прогон «на N часов вперёд»). dt по умолчанию — tick_interval × time_scale.
        Нельзя вызывать, пока работает фоновый поток.
        """
        from .simulator_thread import run_tick

        if self.is_running:
            raise RuntimeError("step() недоступен, пока симулятор запущен в фоновом потоке")
        if dt is None:
            dt = self.time_controller.compute_dt()
        return run_tick(dt, self.store, self.event_bus, self.registry,
                        self.dependency_engine, self.scenario_engine, self.environment)

    # ------------------------------------------------------------------
    # Доступ к данным (п.4.1)
    # ------------------------------------------------------------------

    def get(self, node_id: str, param: str, default: Any = None) -> Any:
        return self.store.get(node_id, param, default)

    def get_node(self, node_id: str) -> dict[str, Any]:
        return self.store.get_node(node_id)

    def get_all(self) -> dict[str, dict[str, Any]]:
        return self.store.get_all()

    def snapshot(self) -> dict[str, Any]:
        return self.store.snapshot()

    def environment_snapshot(self) -> dict[str, Any]:
        """Истинные значения окружения (погода, воздух купола, внешние угрозы).
        Для админки/отладки — участники видят их только через датчики."""
        return self.environment.snapshot()

    def set(self, node_id: str, param: str, value: Any) -> None:
        self.store.set(node_id, param, value)

    def update(self, node_id: str, **kwargs: Any) -> None:
        self.store.update(node_id, **kwargs)

    # ------------------------------------------------------------------
    # Кризисы (п.4.1)
    # ------------------------------------------------------------------

    def trigger_crisis(self, name: str, params: Optional[dict[str, Any]] = None) -> None:
        self.scenario_engine.trigger_crisis(name, self.store, self.event_bus, params)

    def stop_crisis(self, name: str) -> None:
        self.scenario_engine.stop_crisis(name, self.store, self.event_bus)

    def active_crises(self) -> list[str]:
        return self.scenario_engine.active_crises()

    # ------------------------------------------------------------------
    # Узлы: регистрация и управление (не в 4.1 дословно, но необходимо практически)
    # ------------------------------------------------------------------

    def add_node(self, node: BaseNode) -> None:
        self.registry.register(node)

    def add_node_from_type(self, type_name: str, node_id: str, **config: Any) -> BaseNode:
        node = create_node(type_name, node_id, **config)
        self.registry.register(node)
        return node

    def remove_node(self, node_id: str) -> None:
        self.registry.unregister(node_id)

    def node_ids(self) -> list[str]:
        return self.registry.node_ids()

    def describe_nodes(self) -> list[dict[str, Any]]:
        """Каталог узлов: id, тип, название, система и доступные управляющие воздействия."""
        catalog = []
        for node in self.registry.all_nodes():
            catalog.append({
                "node_id": node.node_id,
                "code_name": node.code_name,
                "title": getattr(node, "title", ""),
                "system": getattr(node, "system", ""),
                "category": node.category,
                "controls": node.available_controls() if isinstance(node, SandboxNode) else [],
                "control_aliases": node.control_aliases() if isinstance(node, SandboxNode) else {},
            })
        return catalog

    def control(self, node_id: str, action: str, value: Any = None,
                job_name: Optional[str] = None, duration_s: Optional[float] = None) -> None:
        """
        Единая точка для управляющих команд участников хакатона.

        Команда выполняется узлом (SandboxNode.handle_control) под блокировкой
        тика. Неизвестное действие или недопустимое значение — ControlError
        (подкласс ValueError) с перечнем допустимых вариантов.

        job_name/duration_s — совместимость со старым API: превращаются в
        value={"file_name": job_name, "duration_s": duration_s} (действие start_job).
        """
        node = self.registry.get(node_id)  # бросит KeyError, если узла нет — это ожидаемо
        if job_name is not None and value is None:
            value = {"file_name": job_name}
            if duration_s is not None:
                value["duration_s"] = duration_s

        if not isinstance(node, SandboxNode):
            node.apply_control(action, value)
            return
        with self.store.transaction():
            events = node.handle_control(self.store, action, value)
        for event in events:
            self.event_bus.publish(event)

    # ------------------------------------------------------------------
    # События
    # ------------------------------------------------------------------

    def subscribe(self, event_type: str, callback: EventCallback) -> None:
        self.event_bus.subscribe(event_type, callback)

    def subscribe_all(self, callback: EventCallback) -> None:
        self.event_bus.subscribe_all(callback)

    def recent_events(self, n: int = 50, event_type: Optional[str] = None) -> list[Event]:
        return self.event_bus.get_recent(n=n, event_type=event_type)

    # ------------------------------------------------------------------
    # Правила зависимостей и сценарии (доступ для расширения "снаружи")
    # ------------------------------------------------------------------

    def add_dependency_rule(self, name: str, func, enabled: bool = True) -> None:
        self.dependency_engine.add_rule(name, func, enabled)

    def register_crisis_type(self, name: str, factory) -> None:
        self.scenario_engine.register_scenario_type(name, factory)

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def save_snapshot(self, path: str | Path) -> None:
        self.store.dump_json(path)

    def load_snapshot(self, path: str | Path) -> None:
        self.store.load_json(path)
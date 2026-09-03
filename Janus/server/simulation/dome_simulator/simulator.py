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
from .events import Event, EventBus, EventCallback
from .nodes.base import BaseNode, NodeRegistry, create_node
from .nodes.production import _JobRunnerMixin
from .scenario_engine import ScenarioEngine
from .store import NodeNotFoundError, StateStore
from .time_control import TimeController

logger = logging.getLogger("dome_simulator.simulator")


# Простые "action -> (field, value)" мэппинги для управляющих команд,
# не требующих валидации/вычислений (см. пояснение в части 3).
# Если поле есть в state узла — просто выставляем его.
_ACTION_TO_FIELD: dict[str, tuple[str, Any]] = {
    "enable": ("enabled", True),
    "disable": ("enabled", False),
    "start": ("running", True),
    "stop": ("running", False),
    "shed": ("shed", True),
    "unshed": ("shed", False),
    "arm": ("armed", True),
    "disarm": ("armed", False),
    "trigger": ("triggered", True),
    "reset": ("triggered", False),
    "activate": ("active", True),
    "deactivate": ("active", False),
    "disconnect": ("online", False),
    "reconnect": ("online", True),
    "force_reset": ("status", "ok"),
    "start_processing": ("processing", True),
    "stop_processing": ("processing", False),
    "cancel_job": None,  # обрабатывается отдельной веткой ниже
}


class Simulator:
    """Фасад. Владеет жизненным циклом всех подсистем купола."""

    def __init__(self, tick_interval: float = 4.0, time_scale: float = 1.0) -> None:
        self.store = StateStore()
        self.event_bus = EventBus()
        self.registry = NodeRegistry(self.store)
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

    def control(self, node_id: str, action: str, value: Any = None,
                job_name: Optional[str] = None, duration_s: Optional[float] = None) -> None:
        """
        Единая точка для управляющих команд участников хакатона.

        Простые команды (enable/disable/start/stop/...) сразу пишутся в StateStore
        по таблице _ACTION_TO_FIELD. Команды, требующие вычислений (start_job,
        set_target_output_kw, refuel и т.п.), обрабатываются явно ниже.
        """
        node = self.registry.get(node_id)  # бросит KeyError, если узла нет — это ожидаемо
        current = self.store.get_node(node_id)

        if action == "start_job":
            if not isinstance(node, _JobRunnerMixin):
                raise TypeError(f"Узел {node_id!r} не поддерживает задания (не JobRunner)")
            if not job_name or not duration_s:
                raise ValueError("Для start_job нужны job_name и duration_s")
            node._start_job(node_id, self.store, job_name, float(duration_s))
            return

        if action == "cancel_job":
            if "job_remaining_s" not in current:
                raise TypeError(f"Узел {node_id!r} не выполняет задания")
            self.store.update(node_id, status="idle", job_name=None, job_remaining_s=0.0)
            return

        if action == "set_target_output_kw":
            self.store.set(node_id, "control_target_output_kw", float(value))
            return

        if action == "set_max_discharge_kw":
            self.store.set(node_id, "control_max_discharge_kw", float(value))
            return

        if action == "refuel":
            capacity = getattr(node, "fuel_capacity_l", current.get("fuel_l", 0.0))
            self.store.set(node_id, "fuel_l", capacity)
            return

        if action == "set_target_temp":
            self.store.set(node_id, "target_temp_c", float(value))
            return

        if action == "apply_shock":
            self.store.set(node_id, "external_shock_c", float(value))
            return

        if action == "replace":  # AirFilter
            if "clog_pct" not in current:
                raise TypeError(f"Узел {node_id!r} не поддерживает replace")
            self.store.update(node_id, clog_pct=0.0, efficiency=1.0)
            return

        if action == "degrade":  # NetworkNode
            if "latency_ms" not in current:
                raise TypeError(f"Узел {node_id!r} не поддерживает degrade")
            self.store.update(node_id, latency_ms=float(value or 500.0), packet_loss_pct=50.0)
            return

        mapping = _ACTION_TO_FIELD.get(action)
        if mapping is not None:
            field, mapped_value = mapping
            if field in current:
                self.store.set(node_id, field, mapped_value)
                return

        # Явных обработчиков нет — отдаём узлу (обычно бросит осмысленную ошибку)
        node.apply_control(action, value)

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
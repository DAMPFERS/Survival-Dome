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

Плюс то, что необходимо практически: регистрация узлов, подписка на события,
управляющие команды (control), персистентность (dump/load snapshot), смена
(start_shift, shift_report), команды для реального железа (prepare_real_command,
drain_real_commands) и демо-режим энергосистемы.

Истина и телеметрия:
    get/get_node/get_all       — истинное состояние (узлы, правила, админка);
    snapshot()                 — телеметрия, которую видит агент: с подменами
                                 показаний, заморозками и updated_at (telemetry.py);
    snapshot(view="truth")     — истинное состояние в том же формате.

Команды: control(node_id, action, value, origin=...). origin="agent" — команда
ИИ-агента: проходит только при наличии связи с узлом и не может быть
физическим действием Оператора. Остальные origin (admin, operator) — без
ограничений. До исполнения команду видят активные кризисы (перехват, Д7),
после — оценивают реакцию агента.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .context import Command, SimContext
from .dependency_engine import DependencyEngine
from .environment import Environment
from .events import Event, EventBus, EventCallback, Severity
from .nodes.base import BaseNode, NodeRegistry, create_node
from .nodes.sandbox import ControlError, SandboxNode
from .scenario_engine import ScenarioEngine
from .store import StateStore
from .telemetry import EDGE_SERVED_NODES, TelemetryLayer
from .time_control import TimeController

logger = logging.getLogger("dome_simulator.simulator")

SERVICE_ACTIONS = ("reconnect",)
TIME_MODELS = ("hybrid", "legacy")


class Simulator:
    """Фасад. Владеет жизненным циклом всех подсистем купола."""

    def __init__(self, tick_interval: float = 4.0, time_scale: float = 1.0,
                 day_length_s: float = 10800.0, seed: Optional[int] = None,
                 time_model: str = "hybrid", start_hour: float = 8.0) -> None:
        if time_model not in TIME_MODELS:
            raise ValueError(f"time_model: ожидалось одно из {TIME_MODELS}")
        self.seed = seed
        self.time_model = time_model
        self.store = StateStore()
        self.event_bus = EventBus()
        self.environment = Environment(day_length_s=day_length_s, start_hour=start_hour, seed=seed,
                                       device_time_factor=1.0 if time_model == "hybrid" else None)
        self.registry = NodeRegistry(self.store, self.environment)
        self.dependency_engine = DependencyEngine()
        self.scenario_engine = ScenarioEngine()
        self.time_controller = TimeController(tick_interval=tick_interval, time_scale=time_scale)
        self.telemetry = TelemetryLayer(self.store, self.registry, self.environment, seed=seed)
        self.ctx = SimContext(self.store, self.event_bus, self.environment, self.registry, self.telemetry)
        self.ctx.scenario_engine = self.scenario_engine
        self.ctx.simulator = self
        self._node_specs: list[tuple[str, str, dict[str, Any]]] = []
        self._fault_rates: dict[tuple[str, str], float] = {}
        self.shift: Any = None                # активная смена (shift.ShiftRun)

        self._thread: Optional[Any] = None  # SimulatorThread, создаётся в start()

    # ------------------------------------------------------------------
    # Жизненный цикл (п.4.1)
    # ------------------------------------------------------------------

    def start(self) -> None:
        from .simulator_thread import SimulatorThread  # локальный импорт: избегаем цикла модулей

        if self._thread is not None and self._thread.is_alive():
            logger.warning("Simulator уже запущен")
            return
        self._thread = SimulatorThread(self)
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
        return run_tick(dt, self)

    def run_for(self, seconds: float, dt: Optional[float] = None) -> None:
        """Синхронный прогон на seconds секунд симуляции (тесты, демо)."""
        dt = dt or self.time_controller.tick_interval * max(self.time_controller.time_scale, 1e-9)
        steps = max(1, int(round(seconds / dt)))
        for _ in range(steps):
            self.step(dt)

    # ------------------------------------------------------------------
    # Доступ к данным (п.4.1)
    # ------------------------------------------------------------------

    def get(self, node_id: str, param: str, default: Any = None) -> Any:
        return self.store.get(node_id, param, default)

    def get_node(self, node_id: str) -> dict[str, Any]:
        return self.store.get_node(node_id)

    def get_all(self) -> dict[str, dict[str, Any]]:
        return self.store.get_all()

    def snapshot(self, view: str = "telemetry") -> dict[str, Any]:
        """view="telemetry" — что видит агент (подмены, заморозки, updated_at);
        view="truth" — истинное состояние узлов."""
        if view == "truth":
            return self.store.snapshot()
        with self.store.transaction():
            nodes = self.telemetry.snapshot()
        return {"timestamp": self.environment.now(), "nodes": nodes}

    def telemetry_view(self, node_id: str) -> dict[str, Any]:
        with self.store.transaction():
            return self.telemetry.node_view(node_id)

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
        with self.store.transaction():
            self.scenario_engine.trigger_crisis(name, self.ctx, params)

    def stop_crisis(self, name: str) -> None:
        with self.store.transaction():
            self.scenario_engine.stop_crisis(name, self.ctx)

    def active_crises(self) -> list[str]:
        return self.scenario_engine.active_crises(include_waiting=True)

    def crisis_catalog(self) -> list[dict[str, Any]]:
        return self.scenario_engine.available()

    def crisis_reports(self) -> list[dict[str, Any]]:
        return self.scenario_engine.reports()

    # ------------------------------------------------------------------
    # Смена (dome_crises.md, раздел 8–9)
    # ------------------------------------------------------------------

    def reset_world(self, seed: Optional[int] = None, start_hour: Optional[float] = None) -> None:
        """Одинаковый старт для всех команд: окружение с тем же seed, узлы — в
        начальном состоянии, кризисы и подмены сняты. Реальные узлы остаются real."""
        with self.store.transaction():
            self.scenario_engine.stop_all(self.ctx)
            self.scenario_engine.clear_history()
            self.telemetry.reset()
            seed = self.seed if seed is None else seed
            self.environment.reset(seed=seed, start_hour=start_hour)
            real = {nid for nid in self.node_ids() if self.ctx.is_real(nid)}
            saved = {nid: self.store.get_node(nid) for nid in real}
            for index, (type_name, node_id, config) in enumerate(self._node_specs):
                self.registry.unregister(node_id)
                node_seed = None if seed is None else seed * 1000 + index
                node = create_node(type_name, node_id, seed=node_seed, **config)
                self.registry.register(node)
                if node_id in saved:
                    self.store.update(node_id, **saved[node_id])
            self.ctx.command_log.clear()
            self.ctx.memory.clear()
            self.ctx.real_commands.clear()
            self._fault_rates.clear()

    def set_random_faults(self, enabled: bool) -> None:
        """Выключение случайных отказов узлов и окружения (равные условия смены)."""
        targets: list[tuple[str, Any]] = [(nid, self.registry.get(nid)) for nid in self.node_ids()]
        targets.append(("environment", self.environment))
        for owner, obj in targets:
            for attr in list(vars(obj)):
                if attr.endswith("_rate_per_hour") or attr == "start_failure_prob":
                    key = (owner, attr)
                    if not enabled:
                        self._fault_rates.setdefault(key, getattr(obj, attr))
                        setattr(obj, attr, 0.0)
                    elif key in self._fault_rates:
                        setattr(obj, attr, self._fault_rates.pop(key))

    def start_shift(self, scenario: str = "shift_90", params: Optional[dict[str, Any]] = None) -> None:
        """Сброс мира и запуск сценария смены (одинакового для всех команд)."""
        from .shift import start_shift
        start_shift(self, scenario, params or {})

    def shift_report(self) -> dict[str, Any]:
        from .shift import shift_report
        return shift_report(self)

    # ------------------------------------------------------------------
    # Узлы: регистрация и управление
    # ------------------------------------------------------------------

    def add_node(self, node: BaseNode) -> None:
        self.registry.register(node)

    def add_node_from_type(self, type_name: str, node_id: str, **config: Any) -> BaseNode:
        node = create_node(type_name, node_id, **config)
        self.registry.register(node)
        self._node_specs.append((type_name, node_id, {k: v for k, v in config.items() if k != "seed"}))
        return node

    def remove_node(self, node_id: str) -> None:
        self.registry.unregister(node_id)
        self._node_specs = [s for s in self._node_specs if s[1] != node_id]

    def node_ids(self) -> list[str]:
        return self.registry.node_ids()

    def describe_nodes(self) -> list[dict[str, Any]]:
        """Каталог узлов: id, тип, название, система и доступные управляющие воздействия."""
        catalog = []
        for node in self.registry.all_nodes():
            sandbox = isinstance(node, SandboxNode)
            catalog.append({
                "node_id": node.node_id,
                "code_name": node.code_name,
                "title": getattr(node, "title", ""),
                "system": getattr(node, "system", ""),
                "category": node.category,
                "controls": node.available_controls() if sandbox else [],
                "control_aliases": node.control_aliases() if sandbox else {},
                "operator_controls": node.physical_controls() if sandbox else [],
                "service_actions": list(SERVICE_ACTIONS),
            })
        return catalog

    def resolve_action(self, node_id: str, action: str) -> Optional[str]:
        """Основное имя управляющего воздействия узла (алиасы и регистр нормализуются).
        None — узел такого действия не поддерживает."""
        if action.strip().lower() in SERVICE_ACTIONS:
            return action.strip().lower()
        node = self.registry.get(node_id)
        if not isinstance(node, SandboxNode):
            return None
        return node.canonical_control(action)

    def _prepare(self, node_id: str, action: str, value: Any, origin: str) -> tuple[Any, Command]:
        node = self.registry.get(node_id)  # бросит KeyError, если узла нет — это ожидаемо
        service = action.strip().lower() in SERVICE_ACTIONS
        canonical = action.strip().lower() if service else (
            node.canonical_control(action) if isinstance(node, SandboxNode) else action)
        if canonical is None:
            raise ControlError(
                f"Узел {node_id!r} ({node.code_name}) не поддерживает действие {action!r}. "
                f"Доступно: {node.available_controls() if isinstance(node, SandboxNode) else []}")
        if origin == "agent":
            if isinstance(node, SandboxNode) and canonical in node.physical_controls():
                raise ControlError(f"Действие {canonical!r} выполняет Оператор физически — "
                                   "попросите его об этом через свой канал связи")
            reason = self.telemetry.agent_command_block(node_id) if not service else (
                "Нет связи с куполом: основной канал недоступен" if self.telemetry.link_down() else None)
            if reason:
                raise ControlError(reason)
        return node, Command(node_id, canonical, value, origin, self.ctx.now_s)

    def control(self, node_id: str, action: str, value: Any = None,
                job_name: Optional[str] = None, duration_s: Optional[float] = None,
                origin: str = "admin") -> None:
        """
        Единая точка для управляющих команд.

        Команда выполняется узлом (SandboxNode.handle_control) под блокировкой
        тика. Неизвестное действие или недопустимое значение — ControlError
        (подкласс ValueError) с перечнем допустимых вариантов.

        origin: "agent" (ИИ-агент команды), "operator" (физические действия
        Оператора), "admin" (по умолчанию — без ограничений).
        job_name/duration_s — совместимость со старым API: превращаются в
        value={"file_name": job_name, "duration_s": duration_s} (действие start_job).
        """
        if job_name is not None and value is None:
            value = {"file_name": job_name}
            if duration_s is not None:
                value["duration_s"] = duration_s

        node = self.registry.get(node_id)
        if not isinstance(node, SandboxNode):
            node.apply_control(action, value)
            return
        with self.store.transaction():
            node, cmd = self._prepare(node_id, action, value, origin)
            try:
                outcome = self.scenario_engine.intercept(self.ctx, cmd)
                if outcome is not None and outcome.error:
                    raise ControlError(outcome.error)
                if outcome is None and cmd.action not in SERVICE_ACTIONS:
                    if self.ctx.is_real(node_id):
                        for real_action, real_value in node.real_command(self.store, cmd.action, value):
                            self.ctx.real_commands.append((node_id, real_action, real_value))
                    else:
                        for event in node.handle_control(self.store, action, value):
                            self.event_bus.publish(event)
            except ControlError as e:
                cmd.ok, cmd.error = False, str(e)
                raise
            finally:
                self.ctx.log_command(cmd)
                self.scenario_engine.notify(self.ctx, cmd)

    def prepare_real_command(self, node_id: str, action: str, value: Any = None,
                             origin: str = "agent") -> list[tuple[str, Any]]:
        """
        Для узла в режиме real (сервер): проверки связи и физических действий,
        перехват кризисами (Д7), перевод в команды драйвера. Возвращает список
        (action, value), которые сервер должен выполнить на реальном устройстве.
        """
        with self.store.transaction():
            node, cmd = self._prepare(node_id, action, value, origin)
            try:
                outcome = self.scenario_engine.intercept(self.ctx, cmd)
                if outcome is not None:
                    if outcome.error:
                        raise ControlError(outcome.error)
                    return list(outcome.real_actions or [])
                if cmd.action in SERVICE_ACTIONS:
                    return []
                return node.real_command(self.store, cmd.action, value)
            except ControlError as e:
                cmd.ok, cmd.error = False, str(e)
                raise
            finally:
                self.ctx.log_command(cmd)
                self.scenario_engine.notify(self.ctx, cmd)

    def drain_real_commands(self) -> list[tuple[str, str, Any]]:
        """Команды реальным устройствам от автоматики и кризисов (сервер исполняет их)."""
        return self.ctx.drain_real_commands()

    # ------------------------------------------------------------------
    # Энергосистема смены: виртуальная или демо (реальная PV-генерация)
    # ------------------------------------------------------------------

    def set_energy_mode(self, mode: str) -> None:
        """virtual — энергосистема смены виртуальная (одинаковый старт для всех команд);
        demo — виртуальная, но генерация PV берётся с реального инвертора."""
        if mode not in ("virtual", "demo"):
            raise ValueError('energy mode: "virtual" или "demo"')
        self.ctx.energy_mode = mode

    def feed_real_inverter(self, reading: dict[str, Any]) -> None:
        """Показания реального инвертора, пока энергосистема виртуальная: пишутся
        для админки; в демо-режиме pv_power_w становится генерацией купола."""
        self.ctx.real_inverter_reading = dict(reading)
        if reading.get("pv_power_w") is not None:
            self.ctx.real_pv_power_w = float(reading["pv_power_w"])

    # ------------------------------------------------------------------
    # События
    # ------------------------------------------------------------------

    def subscribe(self, event_type: str, callback: EventCallback) -> None:
        self.event_bus.subscribe(event_type, callback)

    def subscribe_all(self, callback: EventCallback) -> None:
        self.event_bus.subscribe_all(callback)

    def recent_events(self, n: int = 50, event_type: Optional[str] = None, view: str = "truth") -> list[Event]:
        """view="telemetry" — метки событий узлов, обслуживаемых edge, сдвинуты
        на clock_offset_s его часов (№20)."""
        events = self.event_bus.get_recent(n=n, event_type=event_type)
        if view != "telemetry":
            return events
        offset = float(self.get("edge_compute_01", "clock_offset_s", 0.0) or 0.0) \
            if "edge_compute_01" in self.node_ids() else 0.0
        if not offset:
            return events
        return [Event(e.type, e.source, e.payload, e.timestamp + offset, e.severity)
                if e.source in EDGE_SERVED_NODES else e for e in events]

    # ------------------------------------------------------------------
    # Правила зависимостей и сценарии (доступ для расширения "снаружи")
    # ------------------------------------------------------------------

    def add_dependency_rule(self, name: str, func, enabled: bool = True) -> None:
        """func(ctx: SimContext, dt: float)."""
        ctx = self.ctx
        self.dependency_engine.add_rule(name, lambda store, bus, dt, f=func: f(ctx, dt), enabled)

    def register_crisis_type(self, name: str, factory, aliases: tuple[str, ...] = ()) -> None:
        self.scenario_engine.register_scenario_type(name, factory, aliases)

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def save_snapshot(self, path: str | Path) -> None:
        self.store.dump_json(path)

    def load_snapshot(self, path: str | Path) -> None:
        self.store.load_json(path)

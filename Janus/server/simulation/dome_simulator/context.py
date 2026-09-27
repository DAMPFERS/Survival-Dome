# dome_simulator/context.py
"""
SimContext — то, чем пользуются правила зависимостей и кризисы.

Даёт доступ к истинному состоянию узлов (StateStore), телеметрии (TelemetryLayer),
окружению и инстансам узлов, а также выполняет команды узлам от имени
автоматики/кризисов. Команда узлу в режиме real (данные с реального железа) не
исполняется в симуляторе, а ставится в очередь реальных команд — её забирает
сервер и передаёт драйверу (Д7, Д19).

Журнал команд (command_log) видят кризисы: по нему считаются время реакции агента
и ошибочные действия (dome_crises.md, раздел 9).
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from .environment import Environment
from .events import Event, EventBus, Severity
from .nodes.base import NodeRegistry
from .nodes.sandbox import ControlError, SandboxNode
from .store import StateStore
from .telemetry import TelemetryLayer, get_path

logger = logging.getLogger("dome_simulator.context")

ORIGINS = ("agent", "operator", "admin", "automation", "crisis", "scenario", "system")


@dataclass
class Command:
    node_id: str
    action: str                 # основное имя действия (canonical)
    value: Any
    origin: str                 # agent | operator | admin | automation | crisis | scenario | system
    t_s: float                  # время симуляции
    ok: bool = True
    error: Optional[str] = None
    intercepted_by: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "action": self.action, "value": self.value, "origin": self.origin,
                "t_s": round(self.t_s, 1), "ok": self.ok, "error": self.error,
                "intercepted_by": self.intercepted_by}


class SimContext:
    def __init__(self, store: StateStore, event_bus: EventBus, env: Environment,
                 registry: NodeRegistry, telemetry: TelemetryLayer) -> None:
        self.store = store
        self.bus = event_bus
        self.env = env
        self.registry = registry
        self.telemetry = telemetry
        self.scenario_engine: Any = None           # выставляет Simulator
        self.simulator: Any = None                 # выставляет Simulator
        self.command_log: deque[Command] = deque(maxlen=2000)
        self.real_commands: deque[tuple[str, str, Any]] = deque()
        self.energy_mode = "virtual"               # virtual | demo (реальная PV-генерация инвертора)
        self.real_pv_power_w: Optional[float] = None
        self.real_inverter_reading: dict[str, Any] = {}
        self.memory: dict[str, Any] = {}           # служебное состояние правил
        self._lock = threading.RLock()

    # ---------------- время ----------------

    @property
    def now_s(self) -> float:
        """Время симуляции, с (реальное время при time_scale=1)."""
        return self.env.sim_time_s

    def clock(self) -> float:
        return self.env.now()

    # ---------------- истина ----------------

    def has(self, node_id: str) -> bool:
        return node_id in self.store.node_ids()

    def node(self, node_id: str) -> dict[str, Any]:
        return self.store.get_node(node_id)

    def get(self, node_id: str, key: str, default: Any = None) -> Any:
        try:
            if "." in key or "[" in key:
                return get_path(self.store.get_node(node_id), key, default)
            return self.store.get(node_id, key, default)
        except KeyError:
            return default

    def update(self, node_id: str, **kwargs: Any) -> None:
        if self.has(node_id):
            self.store.update(node_id, **kwargs)

    def instance(self, node_id: str) -> Any:
        return self.registry.get(node_id)

    def is_real(self, node_id: str) -> bool:
        return self.get(node_id, "control_override_source") == "real"

    def line(self, number: int, panel: str = "smart_panel_01") -> dict[str, Any]:
        return self.store.get(panel, "lines")[number - 1]

    def line_live(self, number: int, panel: str = "smart_panel_01") -> bool:
        node = self.node(panel)
        return node["lines"][number - 1]["state"] == "ON" and node.get("bus_powered", True)

    # ---------------- телеметрия ----------------

    def view(self, node_id: str) -> dict[str, Any]:
        return self.telemetry.live_view(node_id)

    def live_value(self, node_id: str, path: str, default: Any = None) -> Any:
        return self.telemetry.live_value(node_id, path, default)

    def trust(self, node_id: str, path: str) -> str:
        trust = self.get("dome_automation_01", "data_trust", {}) or {}
        return trust.get(f"{node_id}.{path}") or trust.get(f"{node_id}.*") or "TRUSTED"

    # ---------------- события ----------------

    def emit(self, event_type: str, severity: Severity = Severity.INFO, source: str = "simulator",
             **payload: Any) -> None:
        self.bus.publish(Event(type=event_type, source=source, payload=payload, severity=severity,
                               timestamp=self.clock()))

    # ---------------- команды ----------------

    def command(self, node_id: str, action: str, value: Any = None, origin: str = "system",
                quiet: bool = True) -> bool:
        """Команда узлу от автоматики/кризиса/сценария. Возвращает успех.
        Ошибки узла (ControlError) не пробрасываются — автоматика не должна падать."""
        try:
            node = self.registry.get(node_id)
        except KeyError:
            return False
        cmd = Command(node_id, action, value, origin, self.now_s)
        if not isinstance(node, SandboxNode):
            return False
        canonical = node.canonical_control(action) or action
        cmd.action = canonical
        try:
            if self.is_real(node_id):
                for real_action, real_value in node.real_command(self.store, canonical, value):
                    self.real_commands.append((node_id, real_action, real_value))
            else:
                for event in node.handle_control(self.store, action, value):
                    self.bus.publish(event)
        except ControlError as e:
            cmd.ok, cmd.error = False, str(e)
            if not quiet:
                raise
            logger.debug("Команда %s.%s(%r) от %s не выполнена: %s", node_id, action, value, origin, e)
        self.log_command(cmd)
        return cmd.ok

    def log_command(self, cmd: Command) -> None:
        with self._lock:
            self.command_log.append(cmd)

    def commands_since(self, t_s: float, origin: Optional[str] = None) -> list[Command]:
        with self._lock:
            return [c for c in self.command_log if c.t_s >= t_s and (origin is None or c.origin == origin)]

    def drain_real_commands(self) -> list[tuple[str, str, Any]]:
        with self._lock:
            items = list(self.real_commands)
            self.real_commands.clear()
            return items

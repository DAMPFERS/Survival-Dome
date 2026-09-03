# dome_simulator/nodes/infra.py
"""
Скрытые / инфраструктурные узлы. MainController не моделирует физику —
это "мозг", агрегирующий сводные флаги для верхнеуровневых дашбордов
(его удобно тикать последним — гарантируется NodeRegistry-порядком
регистрации в simulator.py, часть 7).
"""
from __future__ import annotations

from typing import Any

from ..events import Event, EventBus, Severity
from ..store import StateStore
from .base import BaseNode, register_node_type


@register_node_type("main_controller")
class MainController(BaseNode):
    category = "infra"

    def get_state(self) -> dict[str, Any]:
        return {
            "overall_status": "nominal",  # nominal | degraded | critical
            "active_crisis_count": 0,
            "last_update_s": 0.0,
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        # Считает сводный статус по всем узлам StateStore (кроме себя).
        all_nodes = state.get_all()
        critical = 0
        for nid, params in all_nodes.items():
            if nid == self.node_id:
                continue
            health = params.get("health")
            if isinstance(health, (int, float)) and health < 0.3:
                critical += 1
            if params.get("status") in {"tripped", "interrupted"}:
                critical += 1

        status = "nominal"
        if critical >= 3:
            status = "critical"
        elif critical >= 1:
            status = "degraded"

        node = state.get_node(self.node_id)
        elapsed = node["last_update_s"] + dt
        state.update(self.node_id, overall_status=status, last_update_s=elapsed)
        return []

    def on_event(self, event: Event) -> None:
        pass


@register_node_type("emergency_lighting")
class EmergencyLighting(BaseNode):
    category = "infra"

    def get_state(self) -> dict[str, Any]:
        return {"active": False, "battery_pct": 100.0}

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        node = state.get_node(self.node_id)
        if node["active"]:
            battery = max(0.0, node["battery_pct"] - 0.05 * dt)
            state.update(self.node_id, battery_pct=round(battery, 2))
        return []

    def on_event(self, event: Event) -> None:
        # Автоматически включается при потере основного питания
        if event.type == "crisis.power_loss":
            self._activate_pending = True

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"activate", "deactivate"}:
            super().apply_control(action, value)


@register_node_type("fire_suppression")
class FireSuppression(BaseNode):
    category = "infra"

    def get_state(self) -> dict[str, Any]:
        return {"armed": True, "triggered": False, "agent_pct": 100.0}

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)
        if node["triggered"] and node["agent_pct"] > 0:
            agent = max(0.0, node["agent_pct"] - 2.0 * dt)
            state.update(self.node_id, agent_pct=round(agent, 2))
            if agent == 0.0:
                events.append(Event(
                    type="node.overload", source=self.node_id,
                    payload={"reason": "suppression_agent_depleted"}, severity=Severity.CRITICAL,
                ))
        return events

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"trigger", "reset", "arm", "disarm"}:
            super().apply_control(action, value)


@register_node_type("structural_integrity")
class StructuralIntegrity(BaseNode):
    """Параметр «износа» купола, п.4.4. Растёт от кризисов и производственных
    вибраций, крайне медленно растёт всегда (естественное старение)."""
    category = "infra"

    def __init__(self, node_id: str, base_wear_rate_s: float = 0.00002) -> None:
        super().__init__(node_id)
        self.base_wear_rate_s = base_wear_rate_s

    def get_state(self) -> dict[str, Any]:
        return {"wear_pct": 0.0, "control_extra_wear_rate": 0.0}

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)
        wear = node["wear_pct"] + (self.base_wear_rate_s + node["control_extra_wear_rate"]) * dt
        wear = min(100.0, wear)
        state.update(self.node_id, wear_pct=round(wear, 4))
        if wear > 80.0:
            events.append(Event(
                type="node.overload", source=self.node_id,
                payload={"reason": "structural_wear_critical", "wear_pct": wear},
                severity=Severity.CRITICAL,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass


@register_node_type("network_node")
class NetworkNode(BaseNode):
    """Связность внутренней сети купола (для ИИ-агентов участников —
    имитирует задержки/обрывы API Gateway при кризисах)."""
    category = "infra"

    def get_state(self) -> dict[str, Any]:
        return {"online": True, "latency_ms": 20.0, "packet_loss_pct": 0.0}

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        return []

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"disconnect", "reconnect", "degrade"}:
            super().apply_control(action, value)
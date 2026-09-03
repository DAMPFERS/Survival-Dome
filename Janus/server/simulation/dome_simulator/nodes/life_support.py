# dome_simulator/nodes/life_support.py
from __future__ import annotations

from typing import Any

from ..events import Event, EventBus, Severity
from ..store import StateStore
from .base import BaseNode, register_node_type


@register_node_type("water_tank")
class WaterTank(BaseNode):
    category = "life_support"

    def __init__(self, node_id: str, capacity_l: float = 5000.0, consumption_l_s: float = 0.05) -> None:
        super().__init__(node_id)
        self.capacity_l = capacity_l
        self.consumption_l_s = consumption_l_s

    def get_state(self) -> dict[str, Any]:
        return {
            "capacity_l": self.capacity_l,
            "level_l": self.capacity_l * 0.7,
            "control_inflow_l_s": 0.0,  # выставляет WaterRecycling через DependencyEngine
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)
        level = node["level_l"] - self.consumption_l_s * dt + node["control_inflow_l_s"] * dt
        level = max(0.0, min(self.capacity_l, level))
        state.update(self.node_id, level_l=round(level, 2))
        if level < self.capacity_l * 0.1:
            events.append(Event(
                type="node.overload", source=self.node_id,
                payload={"reason": "low_water", "level_l": level}, severity=Severity.CRITICAL,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass


@register_node_type("water_recycling")
class WaterRecycling(BaseNode):
    """Перерабатывает стоки обратно в WaterTank.inflow. Эффективность
    падает от health, требует энергии (control_powered)."""
    category = "life_support"

    def __init__(self, node_id: str, capacity_l_s: float = 0.03, power_draw_kw: float = 0.8) -> None:
        super().__init__(node_id)
        self.capacity_l_s = capacity_l_s
        self.power_draw_kw = power_draw_kw

    def get_state(self) -> dict[str, Any]:
        return {
            "capacity_l_s": self.capacity_l_s,
            "power_draw_kw": self.power_draw_kw,
            "health": 1.0,
            "control_powered": True,
            "output_l_s": 0.0,
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        node = state.get_node(self.node_id)
        output = self.capacity_l_s * node["health"] if node["control_powered"] else 0.0
        state.update(self.node_id, output_l_s=round(output, 4))
        return []

    def on_event(self, event: Event) -> None:
        pass


@register_node_type("oxygen_generator")
class OxygenGenerator(BaseNode):
    category = "life_support"

    def __init__(self, node_id: str, output_pct_s: float = 0.01, power_draw_kw: float = 1.5) -> None:
        super().__init__(node_id)
        self.output_pct_s = output_pct_s
        self.power_draw_kw = power_draw_kw

    def get_state(self) -> dict[str, Any]:
        return {
            "o2_pct": 20.9,
            "output_pct_s": self.output_pct_s,
            "power_draw_kw": self.power_draw_kw,
            "control_powered": True,
            "consumption_pct_s": 0.004,  # базовое потребление обитателями
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)
        gen = node["output_pct_s"] * dt if node["control_powered"] else 0.0
        o2 = node["o2_pct"] + gen - node["consumption_pct_s"] * dt
        o2 = max(10.0, min(23.0, o2))
        state.update(self.node_id, o2_pct=round(o2, 3))
        if o2 < 18.0:
            events.append(Event(
                type="node.overload", source=self.node_id,
                payload={"reason": "low_oxygen", "o2_pct": o2}, severity=Severity.CRITICAL,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass


@register_node_type("waste_management")
class WasteManagement(BaseNode):
    category = "life_support"

    def __init__(self, node_id: str, capacity_kg: float = 500.0, accumulation_kg_s: float = 0.01) -> None:
        super().__init__(node_id)
        self.capacity_kg = capacity_kg
        self.accumulation_kg_s = accumulation_kg_s

    def get_state(self) -> dict[str, Any]:
        return {"capacity_kg": self.capacity_kg, "level_kg": 0.0, "processing": False}

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)
        delta = self.accumulation_kg_s * dt
        if node["processing"]:
            delta -= self.accumulation_kg_s * 2.0 * dt  # переработка быстрее накопления
        level = max(0.0, min(self.capacity_kg, node["level_kg"] + delta))
        state.update(self.node_id, level_kg=round(level, 2))
        if level > self.capacity_kg * 0.9:
            events.append(Event(
                type="node.overload", source=self.node_id,
                payload={"reason": "waste_overflow", "level_kg": level}, severity=Severity.WARNING,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"start_processing", "stop_processing"}:
            super().apply_control(action, value)
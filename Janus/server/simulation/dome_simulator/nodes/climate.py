# dome_simulator/nodes/climate.py
from __future__ import annotations

import random
from typing import Any

from ..events import Event, EventBus, Severity
from ..store import StateStore
from .base import BaseNode, register_node_type


@register_node_type("zone_climate")
class ZoneClimate(BaseNode):
    """Температура и давление одной зоны купола. Дрейфует к target,
    скорость дрейфа зависит от исправности VentilationSystem/HumidityControl
    (те выставляют control_* коэффициенты через StateStore)."""
    category = "climate"

    def __init__(self, node_id: str, zone_name: str, target_temp_c: float = 21.0,
                 base_drift_rate: float = 0.02) -> None:
        super().__init__(node_id)
        self.zone_name = zone_name
        self.target_temp_c = target_temp_c
        self.base_drift_rate = base_drift_rate

    def get_state(self) -> dict[str, Any]:
        return {
            "zone_name": self.zone_name,
            "temperature_c": self.target_temp_c,
            "target_temp_c": self.target_temp_c,
            "pressure_kpa": 101.3,
            "control_drift_multiplier": 1.0,  # растёт при поломке вентиляции — тревога
            "external_shock_c": 0.0,          # разовое возмущение от кризиса
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)

        drift = self.base_drift_rate * node["control_drift_multiplier"] * dt
        temp = node["temperature_c"] + (node["target_temp_c"] - node["temperature_c"]) * min(1.0, drift)
        temp += node["external_shock_c"]

        state.update(self.node_id, temperature_c=round(temp, 3), external_shock_c=0.0)

        if abs(temp - node["target_temp_c"]) > 6.0:
            events.append(Event(
                type="crisis.temperature_anomaly", source=self.node_id,
                payload={"zone": self.zone_name, "temperature_c": temp},
                severity=Severity.WARNING,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"set_target_temp", "apply_shock"}:
            super().apply_control(action, value)


@register_node_type("co2_sensor")
class CO2Sensor(BaseNode):
    """Пассивный сенсор — ppm растёт от количества людей/производства и
    падает от работы CO2Scrubber (тот пишет свой control_scrub_rate,
    сенсор просто читает суммарный эффект через DependencyEngine)."""
    category = "climate"

    def __init__(self, node_id: str, base_generation_ppm_s: float = 0.05) -> None:
        super().__init__(node_id)
        self.base_generation_ppm_s = base_generation_ppm_s

    def get_state(self) -> dict[str, Any]:
        return {
            "ppm": 450.0,
            "control_generation_multiplier": 1.0,  # растёт, если больше людей/станков
            "control_scrub_effect_ppm_s": 0.0,      # выставляет DependencyEngine из CO2Scrubber
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)

        gen = self.base_generation_ppm_s * node["control_generation_multiplier"] * dt
        scrub = node["control_scrub_effect_ppm_s"] * dt
        ppm = max(300.0, node["ppm"] + gen - scrub)

        state.update(self.node_id, ppm=round(ppm, 2))

        if ppm > 1500:
            events.append(Event(
                type="crisis.co2_spike", source=self.node_id,
                payload={"ppm": ppm}, severity=Severity.CRITICAL,
            ))
        elif ppm > 1000:
            events.append(Event(
                type="node.overload", source=self.node_id,
                payload={"reason": "elevated_co2", "ppm": ppm}, severity=Severity.WARNING,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass


@register_node_type("co2_scrubber")
class CO2Scrubber(BaseNode):
    """Активный узел. Требует энергию (заявляет как EnergyConsumer через
    отдельную запись демanda, упрощённо — power_draw_kw как метка для
    DependencyEngine). Эффективность падает при поломке (health)."""
    category = "climate"

    def __init__(self, node_id: str, capacity_ppm_s: float = 0.4, power_draw_kw: float = 1.2) -> None:
        super().__init__(node_id)
        self.capacity_ppm_s = capacity_ppm_s
        self.power_draw_kw = power_draw_kw

    def get_state(self) -> dict[str, Any]:
        return {
            "capacity_ppm_s": self.capacity_ppm_s,
            "power_draw_kw": self.power_draw_kw,
            "enabled": True,
            "health": 1.0,
            "control_powered": True,  # выставляется DependencyEngine по факту наличия энергии
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        node = state.get_node(self.node_id)
        active = node["enabled"] and node["control_powered"]
        effect = self.capacity_ppm_s * node["health"] if active else 0.0
        # эффект публикуется наружу для CO2Sensor через сам StateStore узла —
        # DependencyEngine прочитает scrubber.effect_ppm_s и перенесёт его
        # в co2_sensor.control_scrub_effect_ppm_s (см. часть 5).
        state.update(self.node_id, effect_ppm_s=round(effect, 4))
        return []

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"enable", "disable"}:
            super().apply_control(action, value)


@register_node_type("humidity_control")
class HumidityControl(BaseNode):
    category = "climate"

    def __init__(self, node_id: str, target_humidity_pct: float = 45.0) -> None:
        super().__init__(node_id)
        self.target_humidity_pct = target_humidity_pct

    def get_state(self) -> dict[str, Any]:
        return {
            "humidity_pct": self.target_humidity_pct,
            "target_humidity_pct": self.target_humidity_pct,
            "enabled": True,
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        node = state.get_node(self.node_id)
        noise = random.uniform(-0.3, 0.3) * dt
        if node["enabled"]:
            hum = node["humidity_pct"] + (node["target_humidity_pct"] - node["humidity_pct"]) * 0.05 + noise
        else:
            hum = node["humidity_pct"] + noise * 3  # без контроля дрейфует сильнее
        state.update(self.node_id, humidity_pct=round(max(10.0, min(95.0, hum)), 2))
        return []

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"enable", "disable", "set_target"}:
            super().apply_control(action, value)


@register_node_type("ventilation_system")
class VentilationSystem(BaseNode):
    """Влияет на скорость температурного дрейфа ZoneClimate и на
    control_generation_multiplier CO2Sensor через DependencyEngine.
    Может выйти из строя (health) — тогда драматически хуже держит климат."""
    category = "climate"

    def __init__(self, node_id: str, airflow_m3_h: float = 500.0) -> None:
        super().__init__(node_id)
        self.airflow_m3_h = airflow_m3_h

    def get_state(self) -> dict[str, Any]:
        return {
            "airflow_m3_h": self.airflow_m3_h,
            "running": True,
            "health": 1.0,
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)
        if node["running"] and node["health"] < 0.4:
            events.append(Event(
                type="node.overload", source=self.node_id,
                payload={"reason": "ventilation_degraded", "health": node["health"]},
                severity=Severity.WARNING,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"start", "stop"}:
            super().apply_control(action, value)


@register_node_type("air_filter")
class AirFilter(BaseNode):
    """Забивается со временем (clog_pct растёт), требует обслуживания
    (apply_control("replace"))."""
    category = "climate"

    def __init__(self, node_id: str, clog_rate_pct_s: float = 0.002) -> None:
        super().__init__(node_id)
        self.clog_rate_pct_s = clog_rate_pct_s

    def get_state(self) -> dict[str, Any]:
        return {"clog_pct": 0.0, "efficiency": 1.0}

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)
        clog = min(100.0, node["clog_pct"] + self.clog_rate_pct_s * dt)
        efficiency = max(0.1, 1.0 - clog / 100.0)
        state.update(self.node_id, clog_pct=round(clog, 2), efficiency=round(efficiency, 3))
        if clog > 90.0:
            events.append(Event(
                type="node.overload", source=self.node_id,
                payload={"reason": "filter_clogged", "clog_pct": clog}, severity=Severity.WARNING,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action != "replace":
            super().apply_control(action, value)
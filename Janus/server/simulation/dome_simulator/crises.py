# dome_simulator/crises.py
"""
Стартовый набор кризисов: crisis.power_loss, crisis.co2_spike,
crisis.temperature_anomaly, crisis.solar_degradation (см. п.4.5 ТЗ).

Важно: crisis.power_loss как СОБЫТИЕ уже публикуется автоматически
energy_balance_rule при реальном дефиците энергии (часть 5). Сценарий
PowerLossCrisis ниже — это ВНЕШНИЙ триггер ("нам искусственно нужен
дефицит энергии прямо сейчас"), который создаёт дефицит, отключая
конкретный источник, а не просто публикует событие постфактум.
"""
from __future__ import annotations

from typing import Any, Optional

from .events import Event, EventBus, Severity
from .scenario_engine import CrisisScenario
from .store import StateStore


class PowerLossCrisis(CrisisScenario):
    """
    params:
        target_node_id: str  — id узла-производителя или линии, которую "вырубает"
        duration_s: float    — на сколько (по умолчанию 90с)
    """
    name = "power_loss"

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        super().__init__(params)
        self.duration_s = float(self.params.get("duration_s", 90.0))
        self.target_node_id: Optional[str] = self.params.get("target_node_id")
        self._previous_state: dict[str, Any] = {}

    def on_start(self, store: StateStore, event_bus: EventBus) -> None:
        if self.target_node_id is None:
            # если конкретная цель не указана — берём первого попавшегося производителя
            for nid, p in store.get_all().items():
                if "output_kw" in p and "control_powered" not in p:
                    self.target_node_id = nid
                    break
        if self.target_node_id is None:
            event_bus.publish(Event(
                type="dependency.violation", source="crisis:power_loss",
                payload={"reason": "no_producer_found"}, severity=Severity.WARNING,
            ))
            return

        node = store.get_node(self.target_node_id)
        # Запоминаем исходную ёмкость/мощность, чтобы честно восстановить на on_end
        if "capacity_kw" in node:
            self._previous_state["capacity_kw"] = node["capacity_kw"]
            store.set(self.target_node_id, "capacity_kw", 0.0)
        elif "max_capacity_kw" in node:
            self._previous_state["status"] = node["status"]
            store.set(self.target_node_id, "status", "disabled")

        event_bus.publish(Event(
            type="crisis.power_loss", source="scenario:power_loss",
            payload={"target": self.target_node_id, "duration_s": self.duration_s},
            severity=Severity.CRITICAL,
        ))

    def on_end(self, store: StateStore, event_bus: EventBus) -> None:
        if self.target_node_id is None:
            return
        for key, value in self._previous_state.items():
            store.set(self.target_node_id, key, value)
        event_bus.publish(Event(
            type="line.restored", source="scenario:power_loss",
            payload={"target": self.target_node_id}, severity=Severity.INFO,
        ))


class CO2SpikeCrisis(CrisisScenario):
    """params: multiplier (default 5.0), duration_s (default 60.0).
    Резко разгоняет генерацию CO2 у всех сенсоров на время кризиса."""
    name = "co2_spike"

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        super().__init__(params)
        self.duration_s = float(self.params.get("duration_s", 60.0))
        self.multiplier = float(self.params.get("multiplier", 5.0))
        self._affected_nodes: list[str] = []

    def on_start(self, store: StateStore, event_bus: EventBus) -> None:
        for nid, p in store.get_all().items():
            if "ppm" in p and "control_generation_multiplier" in p:
                self._affected_nodes.append(nid)
                store.set(nid, "control_generation_multiplier", self.multiplier)
        event_bus.publish(Event(
            type="crisis.co2_spike", source="scenario:co2_spike",
            payload={"multiplier": self.multiplier, "nodes": self._affected_nodes},
            severity=Severity.CRITICAL,
        ))

    def on_end(self, store: StateStore, event_bus: EventBus) -> None:
        # Возврат к 1.0: если вентиляция всё ещё сломана, ventilation_coupling_rule
        # (часть 5) на следующем тике всё равно выставит корректный множитель заново.
        for nid in self._affected_nodes:
            store.set(nid, "control_generation_multiplier", 1.0)


class TemperatureAnomalyCrisis(CrisisScenario):
    """params: shock_c (default +/-8.0), duration_s (default 45.0), zone_node_id (опционально).
    Разовый скачок температуры (external_shock_c читается ZoneClimate.tick(), часть 4)."""
    name = "temperature_anomaly"

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        super().__init__(params)
        self.duration_s = float(self.params.get("duration_s", 45.0))
        self.shock_c = float(self.params.get("shock_c", 8.0))
        self.zone_node_id: Optional[str] = self.params.get("zone_node_id")

    def on_start(self, store: StateStore, event_bus: EventBus) -> None:
        targets = [self.zone_node_id] if self.zone_node_id else [
            nid for nid, p in store.get_all().items() if "temperature_c" in p
        ]
        for nid in targets:
            store.set(nid, "external_shock_c", self.shock_c)
        event_bus.publish(Event(
            type="crisis.temperature_anomaly", source="scenario:temperature_anomaly",
            payload={"shock_c": self.shock_c, "zones": targets}, severity=Severity.WARNING,
        ))

    # external_shock_c одноразовый и сам обнуляется в ZoneClimate.tick(),
    # поэтому on_tick/on_end здесь не нужны — эффект уже "разово впрыснут" в on_start.


class SolarDegradationCrisis(CrisisScenario):
    """params: severity (0..1, доля потери health в секунду), duration_s (default 120.0).
    Плавно снижает health всех SolarInverter, пока кризис активен, затем оставляет
    как есть (панели физически повреждены — восстановление требует ремонта, не
    автоматического on_end)."""
    name = "solar_degradation"

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        super().__init__(params)
        self.duration_s = float(self.params.get("duration_s", 120.0))
        self.severity = float(self.params.get("severity", 0.002))  # доля health/сек

    def on_start(self, store: StateStore, event_bus: EventBus) -> None:
        event_bus.publish(Event(
            type="crisis.solar_degradation", source="scenario:solar_degradation",
            payload={"severity": self.severity}, severity=Severity.WARNING,
        ))

    def on_tick(self, store: StateStore, event_bus: EventBus, dt: float) -> None:
        for nid, p in store.get_all().items():
            if "irradiance" in p and "health" in p:  # SolarInverter
                new_health = max(0.0, p["health"] - self.severity * dt)
                store.set(nid, "health", round(new_health, 4))

    # on_end намеренно пустой: деградация панелей необратима без апарат.ремонта
    # (это осознанный элемент "выживальщицкой" сложности сценария для хакатона).


def register_default_crises(engine) -> None:
    """engine: ScenarioEngine. Регистрирует все 4 стартовых кризиса из п.4.5 ТЗ."""
    engine.register_scenario_type("power_loss", lambda params: PowerLossCrisis(params))
    engine.register_scenario_type("co2_spike", lambda params: CO2SpikeCrisis(params))
    engine.register_scenario_type("temperature_anomaly", lambda params: TemperatureAnomalyCrisis(params))
    engine.register_scenario_type("solar_degradation", lambda params: SolarDegradationCrisis(params))
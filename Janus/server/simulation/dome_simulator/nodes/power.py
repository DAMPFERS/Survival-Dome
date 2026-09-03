# dome_simulator/nodes/power.py
"""
Энергетика: производитель (SolarInverter), накопитель (BatteryBank),
передающий элемент (PowerLine).

Эти три класса задают паттерн, по которому далее пишутся
DieselGenerator и EnergyConsumer (часть 4), а также узлы других категорий.
"""
from __future__ import annotations

import math
from typing import Any

from ..events import Event, EventBus, Severity
from ..store import StateStore
from .base import BaseNode, register_node_type


@register_node_type("solar_inverter")
class SolarInverter(BaseNode):
    """
    Производитель энергии. Мощность зависит от суточного цикла (через sim_time,
    накопленный в StateStore самим инвертором) и от health (деградация панелей,
    на неё влияет кризис crisis.solar_degradation через on_event).
    """
    category = "power"

    def __init__(self, node_id: str, capacity_kw: float = 10.0, day_length_s: float = 240.0) -> None:
        super().__init__(node_id)
        self.capacity_kw = capacity_kw
        self.day_length_s = day_length_s  # длительность "суток" симуляции, сек

    def get_state(self) -> dict[str, Any]:
        return {
            "capacity_kw": self.capacity_kw,
            "output_kw": 0.0,
            "irradiance": 0.0,       # 0..1, вычисляется из фазы суток
            "health": 1.0,           # 0..1, деградация панелей
            "elapsed_s": 0.0,        # внутренние "часы" узла
            "control_disabled": False,
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)

        if node["control_disabled"]:
            state.update(self.node_id, output_kw=0.0, irradiance=0.0)
            return events

        elapsed = node["elapsed_s"] + dt
        # синусоида "день/ночь": 0 ночью, пик в середине дня
        phase = (elapsed % self.day_length_s) / self.day_length_s
        irradiance = max(0.0, math.sin(phase * math.pi))

        output_kw = self.capacity_kw * irradiance * node["health"]

        state.update(
            self.node_id,
            elapsed_s=elapsed,
            irradiance=round(irradiance, 3),
            output_kw=round(output_kw, 3),
        )

        if node["health"] < 0.3:
            events.append(Event(
                type="node.overload",
                source=self.node_id,
                payload={"reason": "low_panel_health", "health": node["health"]},
                severity=Severity.WARNING,
            ))
        return events

    def on_event(self, event: Event) -> None:
        # Кризис "деградация солнечных панелей" (например, пыльная буря снаружи купола)
        if event.type == "crisis.solar_degradation":
            self._pending_health_hit = event.payload.get("severity", 0.1)

    def apply_control(self, action: str, value: Any = None) -> None:
        if action == "disable":
            self._disabled_flag = bool(value)
        else:
            super().apply_control(action, value)


@register_node_type("battery_bank")
class BatteryBank(BaseNode):
    """
    Накопитель. Заряд/разряд физически "двигает" DependencyEngine
    (пишет charge_kwh напрямую через StateStore, исходя из баланса
    производства/потребления) — сам узел в tick() только:
      - клиппит charge_kwh в границы [0, capacity_kwh],
      - считает self-discharge,
      - деградирует health при глубоких разрядах,
      - публикует battery.critical при низком заряде.
    """
    category = "power"

    def __init__(
        self,
        node_id: str,
        capacity_kwh: float = 50.0,
        self_discharge_rate: float = 0.0005,  # доля ёмкости в секунду
        critical_pct: float = 0.15,
    ) -> None:
        super().__init__(node_id)
        self.capacity_kwh = capacity_kwh
        self.self_discharge_rate = self_discharge_rate
        self.critical_pct = critical_pct

    def get_state(self) -> dict[str, Any]:
        return {
            "capacity_kwh": self.capacity_kwh,
            "charge_kwh": self.capacity_kwh * 0.8,  # стартуем заряженными на 80%
            "charge_pct": 0.8,
            "health": 1.0,
            "control_max_discharge_kw": 20.0,  # ограничение отдачи, можно менять снаружи
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)

        charge = node["charge_kwh"] - self.capacity_kwh * self.self_discharge_rate * dt
        charge = max(0.0, min(self.capacity_kwh, charge))
        charge_pct = charge / self.capacity_kwh if self.capacity_kwh > 0 else 0.0

        health = node["health"]
        # Глубокий разряд (<5%) понемногу убивает батарею
        if charge_pct < 0.05:
            health = max(0.0, health - 0.0005 * dt)

        state.update(self.node_id, charge_kwh=round(charge, 3),
                     charge_pct=round(charge_pct, 3), health=round(health, 4))

        was_critical = node["charge_pct"] < self.critical_pct
        is_critical = charge_pct < self.critical_pct
        if is_critical and not was_critical:
            events.append(Event(
                type="battery.critical",
                source=self.node_id,
                payload={"charge_pct": charge_pct},
                severity=Severity.CRITICAL,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass  # батарея реагирует на власть DependencyEngine, не на прямые события

    def apply_control(self, action: str, value: Any = None) -> None:
        if action == "set_max_discharge_kw":
            # используется, например, ScenarioEngine во время кризиса power_loss,
            # чтобы принудительно ограничить отдачу и "растянуть" батарею
            pass  # запись в state делает вызывающий код через StateStore.set(),
                  # т.к. apply_control для этого узла управляет только через StateStore
        else:
            super().apply_control(action, value)


@register_node_type("power_line")
class PowerLine(BaseNode):
    """
    Линия передачи между двумя узлами. Текущую нагрузку (current_load_kw)
    выставляет DependencyEngine на основе баланса сети — сама линия в tick()
    только проверяет превышение max_capacity_kw и отключается (trip),
    с возможностью авто-восстановления через reset_cooldown_s.
    """
    category = "power"

    def __init__(
        self,
        node_id: str,
        source: str,
        target: str,
        max_capacity_kw: float = 8.0,
        reset_cooldown_s: float = 30.0,
    ) -> None:
        super().__init__(node_id)
        self.source = source
        self.target = target
        self.max_capacity_kw = max_capacity_kw
        self.reset_cooldown_s = reset_cooldown_s

    def get_state(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "max_capacity_kw": self.max_capacity_kw,
            "current_load_kw": 0.0,
            "status": "ok",             # "ok" | "tripped" | "disabled"
            "cooldown_remaining_s": 0.0,
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)

        if node["status"] == "tripped":
            remaining = max(0.0, node["cooldown_remaining_s"] - dt)
            if remaining == 0.0:
                state.update(self.node_id, status="ok", cooldown_remaining_s=0.0)
                events.append(Event(
                    type="line.restored",
                    source=self.node_id,
                    payload={},
                    severity=Severity.INFO,
                ))
            else:
                state.update(self.node_id, cooldown_remaining_s=remaining)
            return events

        if node["status"] == "disabled":
            return events

        load = node["current_load_kw"]
        if load > node["max_capacity_kw"]:
            state.update(
                self.node_id,
                status="tripped",
                cooldown_remaining_s=self.reset_cooldown_s,
                current_load_kw=0.0,
            )
            events.append(Event(
                type="line.tripped",
                source=self.node_id,
                payload={"load_kw": load, "limit_kw": node["max_capacity_kw"]},
                severity=Severity.CRITICAL,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"disable", "enable", "force_reset"}:
            super().apply_control(action, value)
        # Фактическая запись статуса — забота вызывающего кода через StateStore,
        # чтобы не дублировать точку записи телеметрии.
        
        
# dome_simulator/nodes/power.py  (ДОПОЛНЕНИЕ к части 3 — дописать в конец файла)

@register_node_type("diesel_generator")
class DieselGenerator(BaseNode):
    """
    Резервный генератор. Не крутится сам по себе — запускается либо
    вручную (apply_control("start")), либо DependencyEngine-правилом
    при battery.critical. Расходует fuel_l пропорционально выдаваемой
    мощности; при исчерпании топлива — авто-останов с событием.
    """
    category = "power"

    def __init__(
        self,
        node_id: str,
        max_output_kw: float = 15.0,
        fuel_capacity_l: float = 200.0,
        fuel_burn_l_per_kwh: float = 0.3,
    ) -> None:
        super().__init__(node_id)
        self.max_output_kw = max_output_kw
        self.fuel_capacity_l = fuel_capacity_l
        self.fuel_burn_l_per_kwh = fuel_burn_l_per_kwh

    def get_state(self) -> dict[str, Any]:
        return {
            "max_output_kw": self.max_output_kw,
            "output_kw": 0.0,
            "fuel_l": self.fuel_capacity_l,
            "running": False,
            "control_target_output_kw": 0.0,  # задаётся снаружи/DependencyEngine
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(self.node_id)

        if not node["running"]:
            state.update(self.node_id, output_kw=0.0)
            return events

        target = min(node["control_target_output_kw"], node["max_output_kw"])
        fuel_needed = target * self.fuel_burn_l_per_kwh * (dt / 3600.0)

        if node["fuel_l"] <= 0.0:
            state.update(self.node_id, running=False, output_kw=0.0)
            events.append(Event(
                type="node.overload", source=self.node_id,
                payload={"reason": "out_of_fuel"}, severity=Severity.CRITICAL,
            ))
            return events

        fuel_left = max(0.0, node["fuel_l"] - fuel_needed)
        # если топлива не хватает на весь запрошенный target — отдаём сколько можем
        actual_output = target if fuel_left > 0 or node["fuel_l"] >= fuel_needed else \
            node["fuel_l"] / (self.fuel_burn_l_per_kwh * (dt / 3600.0) + 1e-9)

        state.update(self.node_id, fuel_l=round(fuel_left, 3), output_kw=round(actual_output, 3))

        if fuel_left < self.fuel_capacity_l * 0.1:
            events.append(Event(
                type="node.overload", source=self.node_id,
                payload={"reason": "low_fuel", "fuel_l": fuel_left}, severity=Severity.WARNING,
            ))
        return events

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"start", "stop", "set_target_output_kw", "refuel"}:
            super().apply_control(action, value)
        # Фактическая мутация состояния выполняется фасадом Simulator через StateStore,
        # т.к. состоит из простого присваивания (running=True/False, control_target_output_kw=value).


@register_node_type("energy_consumer")
class EnergyConsumer(BaseNode):
    """
    Логическая группа потребителей (например, "жилой блок", "мастерская").
    demand_kw задаётся базовым профилем + случайным шумом, а фактическое
    потребление (actual_kw) может быть урезано DependencyEngine при
    дефиците энергии в сети (load shedding).
    """
    category = "power"

    def __init__(self, node_id: str, base_demand_kw: float = 3.0, priority: int = 5) -> None:
        super().__init__(node_id)
        self.base_demand_kw = base_demand_kw
        self.priority = priority  # 1 = критично (нельзя отключать), 10 = можно отключить первым

    def get_state(self) -> dict[str, Any]:
        return {
            "base_demand_kw": self.base_demand_kw,
            "demand_kw": self.base_demand_kw,
            "actual_kw": self.base_demand_kw,
            "priority": self.priority,
            "shed": False,  # принудительно отключено ради экономии энергии
        }

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        node = state.get_node(self.node_id)
        # лёгкий стохастический профиль потребления (+-15%)
        import random
        demand = self.base_demand_kw * random.uniform(0.85, 1.15)
        actual = 0.0 if node["shed"] else demand
        state.update(self.node_id, demand_kw=round(demand, 3), actual_kw=round(actual, 3))
        return []

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"shed", "unshed"}:
            super().apply_control(action, value)
# dome_simulator/__init__.py
"""
Точка сборки: create_dome_simulator() поднимает Simulator с полным
стартовым набором узлов из ТЗ (п.4.4), правилами зависимостей (часть 5)
и кризисами (часть 6) — то, что участники хакатона получают "из коробки".
"""
from __future__ import annotations

from .simulator import Simulator
from .rules import build_default_rules
from .crises import register_default_crises

# импорт нужен только ради срабатывания @register_node_type в NODE_TYPES
from .nodes import power, climate, life_support, production, infra  # noqa: F401


def create_dome_simulator(tick_interval: float = 4.0, time_scale: float = 1.0) -> Simulator:
    sim = Simulator(tick_interval=tick_interval, time_scale=time_scale)

    # --- Энергетика ---
    sim.add_node_from_type("solar_inverter", "solar_1", capacity_kw=12.0)
    sim.add_node_from_type("solar_inverter", "solar_2", capacity_kw=12.0)
    sim.add_node_from_type("battery_bank", "battery_1", capacity_kwh=60.0)
    sim.add_node_from_type("diesel_generator", "diesel_1", max_output_kw=15.0)
    for i in range(1, 9):  # минимум 8 реальных линий, п.4.4
        sim.add_node_from_type("power_line", f"line_{i}", source="battery_1",
                                target=f"consumer_{i}", max_capacity_kw=8.0)
    sim.add_node_from_type("energy_consumer", "consumer_residential", base_demand_kw=4.0, priority=1)
    sim.add_node_from_type("energy_consumer", "consumer_workshop", base_demand_kw=6.0, priority=6)

    # --- Климат ---
    sim.add_node_from_type("zone_climate", "climate_residential", zone_name="residential")
    sim.add_node_from_type("zone_climate", "climate_workshop", zone_name="workshop")
    sim.add_node_from_type("co2_sensor", "co2_sensor_1")
    sim.add_node_from_type("co2_scrubber", "co2_scrubber_1")
    sim.add_node_from_type("humidity_control", "humidity_1")
    sim.add_node_from_type("ventilation_system", "ventilation_1")
    sim.add_node_from_type("air_filter", "air_filter_1")

    # --- Жизнеобеспечение ---
    sim.add_node_from_type("water_tank", "water_tank_1")
    sim.add_node_from_type("water_recycling", "water_recycling_1")
    sim.add_node_from_type("oxygen_generator", "oxygen_gen_1")
    sim.add_node_from_type("waste_management", "waste_1")

    # --- Производство ---
    sim.add_node_from_type("cnc_mill", "cnc_1")
    sim.add_node_from_type("printer_3d", "printer_1")
    sim.add_node_from_type("soldering_station", "solder_1")
    sim.add_node_from_type("work_station", "workstation_1")

    # --- Скрытые/инфраструктурные ---
    sim.add_node_from_type("main_controller", "main_controller")
    sim.add_node_from_type("emergency_lighting", "emergency_lights_1")
    sim.add_node_from_type("fire_suppression", "fire_suppression_1")
    sim.add_node_from_type("structural_integrity", "structure_1")
    sim.add_node_from_type("network_node", "network_1")

    # --- Правила и кризисы ---
    for name, func in build_default_rules():
        sim.add_dependency_rule(name, func)
    register_default_crises(sim.scenario_engine)

    return sim
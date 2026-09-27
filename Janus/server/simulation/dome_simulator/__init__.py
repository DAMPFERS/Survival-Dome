# dome_simulator/__init__.py
"""
Точка сборки: create_dome_simulator() поднимает Simulator с полным набором
узлов песочницы из реестра dome_sandbox_nodes.md — то, что участники
хакатона получают "из коробки".

Правила зависимостей (rules.py) и кризисы (crises.py) написаны под прежний
набор узлов и пока не подключаются — они будут переписаны под новые узлы.
До этого узлы генерируют данные самостоятельно из общей модели окружения
(environment.py).
"""
from __future__ import annotations

from typing import Optional

from .simulator import Simulator
from .nodes.sandbox import ControlError

# импорт нужен только ради срабатывания @register_node_type в NODE_TYPES
from .nodes import climate, comms, external, power, production, safety, water  # noqa: F401

__all__ = ["ControlError", "Simulator", "create_dome_simulator", "SANDBOX_NODES"]

# (code name, уникальный ID, конфигурация) — порядок регистрации = порядок тика
SANDBOX_NODES: list[tuple[str, str, dict]] = [
    # 1. Энергетика
    ("solar_panels", "solar_panels_01", {}),
    ("solar_inverter", "solar_inverter_01", {}),
    ("battery", "battery_01", {}),
    ("wind_turbine", "wind_turbine_01", {}),
    ("dizel", "dizel_1", {}),
    ("fuel_tank", "fuel_tank_01", {}),
    # 2. Электроснабжение
    ("smart_panel", "smart_panel_01", {}),
    # 3. Производство
    ("printer_3d", "printer_3d_01", {}),
    ("cnc", "cnc_01", {}),
    # 4. Климат и качество воздуха
    ("climate_sensor", "climate_sensor_01", {}),
    ("air_quality_sensor", "air_quality_sensor_01", {}),
    ("smoke_detector", "smoke_detector_01", {}),
    # 5. Вентиляция
    ("fume_extraction", "fume_extraction_01", {}),
    # 6. Метеорология
    ("weather_station", "weather_station_01", {}),
    # Внешние системы
    ("seysmo", "seysmo_01", {}),
    # Водоснабжение и водоподготовка
    ("water_pump", "water_pump_01", {}),
    ("water_tank", "water_tank_01", {}),
    ("water_filter", "water_filter_01", {}),
    ("fire_suppression", "fire_suppression_01", {}),
    # Безопасность и периметр
    ("access_control", "access_control_01", {}),
    ("radiation_sensor", "radiation_sensor_01", {}),
    ("chem_sensor", "chem_sensor_01", {}),
    # 7. Производство и материалы
    ("material_inventory", "material_inventory_01", {}),
    ("equipment_cooling", "equipment_cooling_01", {}),
    # 8. Климат, вентиляция и герметизация
    ("supply_ventilation", "supply_ventilation_01", {}),
    ("dome_sealing", "dome_sealing_01", {}),
    ("climate_sensor_extra", "climate_sensor_02", {"reading_delay_s": 900.0, "drift_rate_c_per_day": 0.15}),
    ("climate_sensor_extra", "climate_sensor_03", {"reading_delay_s": 1800.0, "drift_rate_c_per_day": 0.3}),
    # 9. Связь и вычисления
    ("radio", "radio_01", {}),
    ("backup_comms", "backup_comms_01", {}),
    ("edge_compute", "edge_compute_01", {}),
    # 10. МС-ТЮК
    ("thermal_insulation", "thermal_insulation_01", {}),
]


def create_dome_simulator(tick_interval: float = 4.0, time_scale: float = 1.0,
                          day_length_s: float = 1440.0, seed: Optional[int] = None) -> Simulator:
    """
    day_length_s — длительность игровых суток в секундах симуляции
    (по умолчанию 1440: 1 с симуляции = 1 игровая минута).
    seed — фиксирует генерацию данных (одинаковый прогон для тестов/демо).
    """
    sim = Simulator(tick_interval=tick_interval, time_scale=time_scale,
                    day_length_s=day_length_s, seed=seed)
    for index, (type_name, node_id, config) in enumerate(SANDBOX_NODES):
        node_seed = None if seed is None else seed * 1000 + index
        sim.add_node_from_type(type_name, node_id, seed=node_seed, **config)
    return sim

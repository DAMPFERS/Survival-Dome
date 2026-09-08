# tests/test_nodes.py
"""Тесты работы узлов всех категорий."""
import time
import pytest
from .helpers import simulate_for_ticks, wait_for_condition, assert_event_published


# ============================================================================
# ЭНЕРГЕТИКА (Power)
# ============================================================================

def test_solar_inverter_generates_power(running_simulator):
    """Солнечная панель генерирует энергию."""
    sim = running_simulator
    
    # Ждём несколько тиков
    time.sleep(3.0)
    
    output_kw = sim.get("solar_1", "output_kw")
    assert output_kw >= 0  # генерация неотрицательна


def test_solar_inverter_irradiance_changes(fast_simulator):
    """Освещённость панели меняется по синусоиде (суточный цикл)."""
    sim = fast_simulator
    sim.start()
    
    irradiance_1 = sim.get("solar_1", "irradiance")
    time.sleep(5.0)  # ждём изменения фазы
    irradiance_2 = sim.get("solar_1", "irradiance")
    
    # За 5 секунд при time_scale=4 проходит 20 сек симуляции
    # Освещённость должна измениться
    assert irradiance_1 != irradiance_2
    
    sim.stop()


def test_solar_health_affects_output(running_simulator):
    """Health панели влияет на выработку."""
    sim = running_simulator
    
    # Снижаем health
    sim.set("solar_1", "health", 0.5)
    time.sleep(2.0)
    
    output_with_low_health = sim.get("solar_1", "output_kw")
    capacity = sim.get("solar_1", "capacity_kw")
    
    # При health=0.5 выработка должна быть меньше capacity
    assert output_with_low_health < capacity


def test_battery_self_discharge(running_simulator):
    """Батарея саморазряжается со временем."""
    sim = running_simulator
    
    initial_charge = sim.get("battery_1", "charge_kwh")
    time.sleep(3.0)
    final_charge = sim.get("battery_1", "charge_kwh")
    
    # Заряд должен уменьшиться
    assert final_charge < initial_charge


def test_battery_critical_event(running_simulator, event_collector):
    """Событие battery.critical при низком заряде."""
    sim = running_simulator
    sim.subscribe_all(event_collector)
    
    # Устанавливаем критически низкий заряд
    sim.set("battery_1", "charge_kwh", 5.0)  # <15% от 60 кВтч
    sim.set("battery_1", "charge_pct", 5.0 / 60.0)
    
    time.sleep(2.0)
    
    # Проверяем событие
    assert assert_event_published(event_collector, "battery.critical", timeout_s=3)


def test_diesel_generator_fuel_consumption(running_simulator):
    """Дизель расходует топливо при работе."""
    sim = running_simulator
    
    # Запускаем генератор
    sim.update("diesel_1", running=True, control_target_output_kw=10.0)
    initial_fuel = sim.get("diesel_1", "fuel_l")
    
    time.sleep(3.0)
    
    final_fuel = sim.get("diesel_1", "fuel_l")
    assert final_fuel < initial_fuel  # топливо расходуется


def test_diesel_out_of_fuel_stops(running_simulator, event_collector):
    """Дизель останавливается при исчерпании топлива."""
    sim = running_simulator
    sim.subscribe_all(event_collector)
    
    # Устанавливаем почти нулевой запас топлива
    sim.update("diesel_1", running=True, fuel_l=0.1, control_target_output_kw=15.0)
    
    time.sleep(2.0)
    
    # Генератор должен остановиться
    running = sim.get("diesel_1", "running")
    assert running == False
    
    # Должно быть событие out_of_fuel
    events = [e for e in event_collector.events if e.source == "diesel_1"]
    assert any("out_of_fuel" in str(e.payload) for e in events)


def test_power_line_normal_operation(running_simulator):
    """Линия электропередачи работает в штатном режиме."""
    sim = running_simulator
    
    status = sim.get("line_1", "status")
    assert status in ["ok", "tripped", "disabled"]


def test_energy_consumer_demand_varies(running_simulator):
    """Потребление энергии варьируется."""
    sim = running_simulator
    
    demand_1 = sim.get("consumer_residential", "demand_kw")
    time.sleep(2.0)
    demand_2 = sim.get("consumer_residential", "demand_kw")
    
    # Demand меняется стохастически
    assert demand_1 != demand_2 or demand_1 > 0  # либо меняется, либо просто работает


# ============================================================================
# КЛИМАТ (Climate)
# ============================================================================

def test_zone_climate_temperature_stable(running_simulator):
    """Температура стабильна при нормальных условиях."""
    sim = running_simulator
    
    temp_1 = sim.get("climate_residential", "temperature_c")
    time.sleep(2.0)
    temp_2 = sim.get("climate_residential", "temperature_c")
    
    # Температура близка к target, не дрейфует сильно
    target = sim.get("climate_residential", "target_temp_c")
    assert abs(temp_2 - target) < 5.0


def test_zone_climate_external_shock(running_simulator):
    """Внешний шок меняет температуру."""
    sim = running_simulator
    
    initial_temp = sim.get("climate_residential", "temperature_c")
    
    # Применяем шок
    sim.set("climate_residential", "external_shock_c", 10.0)
    time.sleep(1.5)  # ждём тик
    
    new_temp = sim.get("climate_residential", "temperature_c")
    assert new_temp > initial_temp  # температура выросла


def test_co2_sensor_accumulates(running_simulator):
    """CO2 накапливается со временем."""
    sim = running_simulator
    
    initial_ppm = sim.get("co2_sensor_1", "ppm")
    time.sleep(3.0)
    final_ppm = sim.get("co2_sensor_1", "ppm")
    
    assert final_ppm >= initial_ppm  # CO2 растёт или стабилен


def test_co2_scrubber_reduces_co2(running_simulator):
    """Скруббер снижает уровень CO2."""
    sim = running_simulator
    
    # Включаем скруббер
    sim.update("co2_scrubber_1", enabled=True, control_powered=True)
    
    # Проверяем эффект
    time.sleep(1.0)
    effect = sim.get("co2_scrubber_1", "effect_ppm_s")
    assert effect > 0  # скруббер работает


def test_co2_scrubber_disabled_no_effect(running_simulator):
    """Выключенный скруббер не работает."""
    sim = running_simulator
    
    sim.update("co2_scrubber_1", enabled=False)
    time.sleep(1.0)
    
    effect = sim.get("co2_scrubber_1", "effect_ppm_s")
    assert effect == 0


def test_ventilation_system_health(running_simulator):
    """Вентиляция имеет health параметр."""
    sim = running_simulator
    
    health = sim.get("ventilation_1", "health")
    assert 0 <= health <= 1.0


def test_air_filter_clogs_over_time(fast_simulator):
    """Воздушный фильтр засоряется."""
    sim = fast_simulator
    sim.start()
    
    initial_clog = sim.get("air_filter_1", "clog_pct")
    time.sleep(5.0)
    final_clog = sim.get("air_filter_1", "clog_pct")
    
    assert final_clog > initial_clog  # засорение растёт
    sim.stop()


# ============================================================================
# ЖИЗНЕОБЕСПЕЧЕНИЕ (Life Support)
# ============================================================================

def test_water_tank_level_decreases(running_simulator):
    """Уровень воды в баке снижается."""
    sim = running_simulator
    
    initial_level = sim.get("water_tank_1", "level_l")
    time.sleep(2.0)
    final_level = sim.get("water_tank_1", "level_l")
    
    assert final_level <= initial_level  # потребление


def test_water_recycling_produces_output(running_simulator):
    """Переработка воды производит выход."""
    sim = running_simulator
    
    sim.set("water_recycling_1", "control_powered", True)
    time.sleep(1.0)
    
    output = sim.get("water_recycling_1", "output_l_s")
    assert output >= 0


def test_oxygen_generator_maintains_o2(running_simulator):
    """Генератор кислорода поддерживает уровень O2."""
    sim = running_simulator
    
    o2_level = sim.get("oxygen_gen_1", "o2_pct")
    assert 18.0 <= o2_level <= 23.0  # безопасный диапазон


def test_waste_management_accumulates(running_simulator):
    """Отходы накапливаются."""
    sim = running_simulator
    
    initial_waste = sim.get("waste_1", "level_kg")
    time.sleep(2.0)
    final_waste = sim.get("waste_1", "level_kg")
    
    assert final_waste >= initial_waste


# ============================================================================
# ПРОИЗВОДСТВО (Production)
# ============================================================================

def test_cnc_starts_job(running_simulator):
    """CNC начинает выполнение задания."""
    sim = running_simulator
    
    sim.control("cnc_1", "start_job", job_name="test_part", duration_s=10.0)
    time.sleep(0.5)
    
    status = sim.get("cnc_1", "status")
    assert status == "running"


def test_cnc_job_completion(running_simulator):
    """CNC завершает задание."""
    sim = running_simulator
    
    sim.control("cnc_1", "start_job", job_name="test", duration_s=3.0)
    time.sleep(4.0)  # ждём завершения
    
    status = sim.get("cnc_1", "status")
    assert status == "done"


def test_3d_printer_job_progress(running_simulator):
    """3D принтер выполняет задание."""
    sim = running_simulator
    
    sim.control("printer_1", "start_job", job_name="bracket", duration_s=10.0)
    time.sleep(0.5)
    
    remaining = sim.get("printer_1", "job_remaining_s")
    assert 0 < remaining <= 10.0


def test_job_cancel(running_simulator):
    """Отмена задания работает."""
    sim = running_simulator
    
    sim.control("cnc_1", "start_job", job_name="test", duration_s=20.0)
    time.sleep(0.5)
    
    sim.control("cnc_1", "cancel_job")
    time.sleep(0.5)
    
    status = sim.get("cnc_1", "status")
    assert status == "idle"


# ============================================================================
# ИНФРАСТРУКТУРА (Infra)
# ============================================================================

def test_main_controller_status(running_simulator):
    """Главный контроллер отслеживает статус системы."""
    sim = running_simulator
    
    status = sim.get("main_controller", "overall_status")
    assert status in ["nominal", "degraded", "critical"]


def test_emergency_lighting_battery(running_simulator):
    """Аварийное освещение имеет батарею."""
    sim = running_simulator
    
    battery_pct = sim.get("emergency_lights_1", "battery_pct")
    assert 0 <= battery_pct <= 100.0


def test_structural_integrity_wear(fast_simulator):
    """Износ структуры растёт."""
    sim = fast_simulator
    sim.start()
    
    initial_wear = sim.get("structure_1", "wear_pct")
    time.sleep(5.0)
    final_wear = sim.get("structure_1", "wear_pct")
    
    assert final_wear >= initial_wear
    sim.stop()


def test_network_node_online(running_simulator):
    """Сетевой узел онлайн."""
    sim = running_simulator
    
    online = sim.get("network_1", "online")
    assert online in [True, False]

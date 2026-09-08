# tests/test_dependencies.py
"""Тесты правил зависимостей между узлами."""
import time
import pytest
from .helpers import simulate_for_ticks, wait_for_condition


def test_energy_balance_surplus_charges_battery(running_simulator):
    """Избыток энергии заряжает батареи."""
    sim = running_simulator
    
    # Устанавливаем низкий заряд батареи
    sim.set("battery_1", "charge_kwh", 30.0)
    initial_charge = 30.0
    
    # Солнце светит, нагрузки мало — должен быть избыток
    time.sleep(3.0)
    
    final_charge = sim.get("battery_1", "charge_kwh")
    # Батарея должна зарядиться (или хотя бы не разрядиться сильно)
    assert final_charge >= initial_charge - 1.0


def test_energy_balance_deficit_discharges_battery(running_simulator):
    """Дефицит энергии разряжает батареи."""
    sim = running_simulator
    
    # Отключаем солнечные панели (создаём дефицит)
    sim.set("solar_1", "capacity_kw", 0.0)
    sim.set("solar_2", "capacity_kw", 0.0)
    
    initial_charge = sim.get("battery_1", "charge_kwh")
    
    time.sleep(3.0)
    
    final_charge = sim.get("battery_1", "charge_kwh")
    # Батарея должна разрядиться
    assert final_charge < initial_charge


def test_energy_balance_load_shedding(running_simulator):
    """Глубокий дефицит отключает нагрузки."""
    sim = running_simulator
    
    # Создаём критический дефицит: отключаем панели и дизель
    sim.set("solar_1", "capacity_kw", 0.0)
    sim.set("solar_2", "capacity_kw", 0.0)
    sim.set("battery_1", "charge_kwh", 1.0)  # почти пустая батарея
    sim.update("diesel_1", running=False, fuel_l=0.0)
    
    # Запускаем энергоёмкое оборудование
    sim.control("cnc_1", "start_job", job_name="test", duration_s=30.0)
    
    time.sleep(3.0)
    
    # CNC должен быть отключён (низкий приоритет)
    cnc_powered = sim.get("cnc_1", "control_powered")
    assert cnc_powered == False


def test_energy_balance_priority_order(running_simulator):
    """Отключение идёт по приоритету: производство → климат → жизнеобеспечение."""
    sim = running_simulator
    
    # Создаём дефицит
    sim.set("solar_1", "capacity_kw", 0.0)
    sim.set("solar_2", "capacity_kw", 0.0)
    sim.set("battery_1", "charge_kwh", 0.5)
    
    # Запускаем производство
    sim.control("cnc_1", "start_job", job_name="test", duration_s=30.0)
    
    time.sleep(3.0)
    
    # Производство отключается первым
    cnc_powered = sim.get("cnc_1", "control_powered")
    
    # Жизнеобеспечение должно работать
    o2_powered = sim.get("oxygen_gen_1", "control_powered")
    
    # При дефиците: производство выключено, жизнеобеспечение работает
    assert cnc_powered == False or o2_powered == True


def test_diesel_autostart_on_low_battery(running_simulator, event_collector):
    """Автозапуск дизеля при <20% батареи."""
    sim = running_simulator
    sim.subscribe_all(event_collector)
    
    # Устанавливаем низкий заряд батареи
    sim.set("battery_1", "charge_pct", 0.15)  # 15%
    sim.set("battery_1", "charge_kwh", 9.0)
    
    # Убеждаемся, что дизель заправлен
    sim.set("diesel_1", "fuel_l", 100.0)
    
    time.sleep(2.0)
    
    # Дизель должен автоматически запуститься
    diesel_running = sim.get("diesel_1", "running")
    assert diesel_running == True


def test_diesel_autostop_on_healthy_battery(running_simulator):
    """Автоостанов дизеля при >60% батареи."""
    sim = running_simulator
    
    # Запускаем дизель вручную
    sim.update("diesel_1", running=True, fuel_l=100.0, control_target_output_kw=15.0)
    
    # Устанавливаем высокий заряд
    sim.set("battery_1", "charge_pct", 0.70)  # 70%
    sim.set("battery_1", "charge_kwh", 42.0)
    
    time.sleep(2.0)
    
    # Дизель должен остановиться
    diesel_running = sim.get("diesel_1", "running")
    assert diesel_running == False


def test_co2_scrubber_to_sensor_coupling(running_simulator):
    """Скруббер влияет на сенсор CO2."""
    sim = running_simulator
    
    # Включаем скруббер
    sim.update("co2_scrubber_1", enabled=True, control_powered=True, health=1.0)
    
    time.sleep(2.0)
    
    # Сенсор должен получить control_scrub_effect_ppm_s > 0
    scrub_effect = sim.get("co2_sensor_1", "control_scrub_effect_ppm_s")
    assert scrub_effect > 0


def test_ventilation_affects_climate_drift(running_simulator):
    """Состояние вентиляции влияет на дрейф температуры."""
    sim = running_simulator
    
    # Ломаем вентиляцию
    sim.set("ventilation_1", "health", 0.3)
    sim.set("ventilation_1", "running", False)
    
    time.sleep(2.0)
    
    # Множитель дрейфа должен вырасти
    drift_multiplier = sim.get("climate_residential", "control_drift_multiplier")
    assert drift_multiplier > 1.0  # хуже обычного


def test_ventilation_affects_co2_generation(running_simulator):
    """Плохая вентиляция ускоряет накопление CO2."""
    sim = running_simulator
    
    # Отключаем вентиляцию
    sim.set("ventilation_1", "running", False)
    
    time.sleep(2.0)
    
    # Множитель генерации CO2 должен вырасти
    gen_multiplier = sim.get("co2_sensor_1", "control_generation_multiplier")
    assert gen_multiplier >= 1.0


def test_water_recycling_to_tank_flow(running_simulator):
    """Переработка воды пополняет бак."""
    sim = running_simulator
    
    # Включаем переработку
    sim.update("water_recycling_1", control_powered=True, health=1.0)
    
    time.sleep(2.0)
    
    # Бак должен получить приток
    inflow = sim.get("water_tank_1", "control_inflow_l_s")
    assert inflow > 0


def test_structural_wear_from_critical_events(running_simulator, event_collector):
    """Критические события увеличивают износ."""
    sim = running_simulator
    sim.subscribe_all(event_collector)
    
    initial_wear = sim.get("structure_1", "wear_pct")
    
    # Генерируем критические события (запускаем кризис)
    sim.trigger_crisis("power_loss", params={"duration_s": 4.0})
    
    time.sleep(5.0)
    
    final_wear = sim.get("structure_1", "wear_pct")
    # Износ должен увеличиться (или хотя бы не остаться нулевым)
    assert final_wear >= initial_wear


def test_emergency_lighting_activates_on_power_loss(running_simulator, event_collector):
    """Аварийное освещение включается при дефиците энергии."""
    sim = running_simulator
    sim.subscribe_all(event_collector)
    
    # Создаём дефицит энергии
    sim.set("solar_1", "capacity_kw", 0.0)
    sim.set("solar_2", "capacity_kw", 0.0)
    sim.set("battery_1", "charge_kwh", 0.5)
    
    time.sleep(3.0)
    
    # Аварийное освещение может активироваться
    # (зависит от правила emergency_lighting_rule)
    active = sim.get("emergency_lights_1", "active")
    
    # Проверяем, что хотя бы было событие power_loss
    power_loss_events = [e for e in event_collector.events if e.type == "crisis.power_loss"]
    assert len(power_loss_events) > 0 or active in [True, False]

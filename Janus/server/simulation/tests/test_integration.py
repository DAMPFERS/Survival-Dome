# tests/test_integration.py
"""Интеграционные тесты комплексных сценариев."""
import time
import pytest
from .helpers import simulate_for_ticks, wait_for_crisis_end, collect_parameter_over_time


def test_full_day_night_cycle(fast_simulator):
    """Полный суточный цикл без кризисов."""
    sim = fast_simulator
    sim.start()
    
    # Собираем данные о выработке панелей
    values = []
    for _ in range(10):
        output = sim.get("solar_1", "output_kw")
        irradiance = sim.get("solar_1", "irradiance")
        values.append((output, irradiance))
        time.sleep(0.5)
    
    # Проверяем, что выработка меняется
    outputs = [v[0] for v in values]
    assert len(set(outputs)) > 1  # значения различаются
    
    sim.stop()


def test_production_workflow(running_simulator):
    """Запуск серии заданий на производстве."""
    sim = running_simulator
    
    # Запускаем задания на разных станках
    sim.control("cnc_1", "start_job", job_name="part_1", duration_s=5.0)
    sim.control("printer_1", "start_job", job_name="bracket", duration_s=6.0)
    sim.control("solder_1", "start_job", job_name="board", duration_s=4.0)
    
    time.sleep(1.0)
    
    # Все должны работать
    assert sim.get("cnc_1", "status") == "running"
    assert sim.get("printer_1", "status") == "running"
    assert sim.get("solder_1", "status") == "running"
    
    # Ждём завершения самого короткого
    time.sleep(5.0)
    
    # Solder должен завершиться первым
    solder_status = sim.get("solder_1", "status")
    assert solder_status in ["done", "idle"]


def test_energy_crisis_recovery(running_simulator, event_collector):
    """Кризис энергии → автозапуск дизеля → восстановление."""
    sim = running_simulator
    sim.subscribe_all(event_collector)
    
    # Устанавливаем низкий заряд батареи
    sim.set("battery_1", "charge_pct", 0.15)
    sim.set("battery_1", "charge_kwh", 9.0)
    sim.set("diesel_1", "fuel_l", 150.0)
    
    time.sleep(2.0)
    
    # Дизель должен запуститься автоматически
    diesel_running = sim.get("diesel_1", "running")
    assert diesel_running == True
    
    # Батарея начинает заряжаться
    time.sleep(3.0)
    
    charge_after = sim.get("battery_1", "charge_kwh")
    assert charge_after > 9.0  # зарядилась


def test_co2_crisis_with_scrubber_response(running_simulator):
    """Кризис CO2 → включение скруббера → стабилизация."""
    sim = running_simulator
    
    # Запускаем кризис CO2
    sim.trigger_crisis("co2_spike", {"multiplier": 5.0, "duration_s": 5.0})
    
    # Включаем скруббер на полную мощность
    sim.update("co2_scrubber_1", enabled=True, control_powered=True, health=1.0)
    
    initial_ppm = sim.get("co2_sensor_1", "ppm")
    
    time.sleep(6.0)  # ждём окончания кризиса
    
    # CO2 должен стабилизироваться или хотя бы не вырасти сильно
    final_ppm = sim.get("co2_sensor_1", "ppm")
    assert final_ppm < initial_ppm + 500  # рост не катастрофический


def test_cascading_failures(running_simulator, event_collector):
    """Каскадные отказы: power loss → производство прервано → CO2 растёт."""
    sim = running_simulator
    sim.subscribe_all(event_collector)
    
    # Запускаем производство
    sim.control("cnc_1", "start_job", job_name="test", duration_s=20.0)
    time.sleep(1.0)
    
    # Кризис энергии
    sim.trigger_crisis("power_loss", {"target_node_id": "solar_1", "duration_s": 5.0})
    
    time.sleep(3.0)
    
    # Производство должно прерваться или отключиться питанием
    cnc_status = sim.get("cnc_1", "status")
    cnc_powered = sim.get("cnc_1", "control_powered")
    
    # Либо прервано, либо выключено
    assert cnc_status == "interrupted" or cnc_powered == False


def test_operator_intervention(running_simulator):
    """Вмешательство оператора во время кризиса."""
    sim = running_simulator
    
    # Запускаем кризис
    sim.trigger_crisis("power_loss", {"target_node_id": "solar_1", "duration_s": 10.0})
    time.sleep(1.0)
    
    # Оператор вручную запускает дизель
    sim.update("diesel_1", running=True, fuel_l=100.0, control_target_output_kw=15.0)
    
    time.sleep(2.0)
    
    # Дизель работает
    diesel_output = sim.get("diesel_1", "output_kw")
    assert diesel_output > 0
    
    # Оператор останавливает кризис досрочно
    sim.stop_crisis("power_loss")
    time.sleep(1.0)
    
    # Панель восстановлена
    capacity = sim.get("solar_1", "capacity_kw")
    assert capacity > 0


def test_persistent_state_save_load(simulator, tmp_path):
    """Сохранение и загрузка состояния."""
    sim = simulator
    sim.start()
    
    # Устанавливаем специфичное состояние
    sim.set("battery_1", "charge_kwh", 42.0)
    sim.set("diesel_1", "fuel_l", 123.45)
    
    time.sleep(1.0)
    
    # Сохраняем
    snapshot_file = tmp_path / "test_snapshot.json"
    sim.save_snapshot(str(snapshot_file))
    
    sim.stop()
    
    # Создаём новый симулятор и загружаем состояние
    from dome_simulator import create_dome_simulator
    sim2 = create_dome_simulator()
    sim2.load_snapshot(str(snapshot_file))
    
    # Проверяем восстановление
    battery_charge = sim2.get("battery_1", "charge_kwh")
    diesel_fuel = sim2.get("diesel_1", "fuel_l")
    
    assert battery_charge == 42.0
    assert diesel_fuel == 123.45

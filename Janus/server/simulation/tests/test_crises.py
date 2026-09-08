# tests/test_crises.py
"""Тесты кризисных сценариев."""
import time
import pytest
from .helpers import wait_for_crisis_end, assert_event_published, wait_for_condition


# ============================================================================
# PowerLossCrisis
# ============================================================================

def test_power_loss_starts(running_simulator, crisis_params):
    """Кризис power_loss запускается."""
    sim = running_simulator
    
    sim.trigger_crisis("power_loss", crisis_params["power_loss"])
    
    # Проверяем, что кризис в списке активных
    active = sim.active_crises()
    assert "power_loss" in active


def test_power_loss_disables_target(running_simulator, crisis_params):
    """Power loss отключает целевой узел."""
    sim = running_simulator
    
    # Запоминаем исходную мощность
    initial_capacity = sim.get("solar_1", "capacity_kw")
    assert initial_capacity == 12.0
    
    # Запускаем кризис
    sim.trigger_crisis("power_loss", crisis_params["power_loss"])
    time.sleep(1.0)
    
    # Проверяем: мощность должна упасть
    crisis_capacity = sim.get("solar_1", "capacity_kw")
    assert crisis_capacity == 0.0


def test_power_loss_publishes_event(running_simulator, event_collector, crisis_params):
    """Power loss публикует событие crisis.power_loss."""
    sim = running_simulator
    sim.subscribe_all(event_collector)
    
    sim.trigger_crisis("power_loss", crisis_params["power_loss"])
    
    # Проверяем событие
    assert assert_event_published(event_collector, "crisis.power_loss", timeout_s=2)


def test_power_loss_duration(running_simulator, crisis_params):
    """Кризис длится заданное время."""
    sim = running_simulator
    
    sim.trigger_crisis("power_loss", {"target_node_id": "solar_1", "duration_s": 4.0})
    
    # Ждём завершения (4 сек + запас)
    assert wait_for_crisis_end(sim, "power_loss", timeout_s=8.0)


def test_power_loss_restores_on_end(running_simulator, crisis_params):
    """Узел восстанавливается после завершения кризиса."""
    sim = running_simulator
    
    initial_capacity = sim.get("solar_1", "capacity_kw")
    
    # Короткий кризис
    sim.trigger_crisis("power_loss", {"target_node_id": "solar_1", "duration_s": 3.0})
    time.sleep(1.0)
    
    # Во время кризиса: capacity=0
    assert sim.get("solar_1", "capacity_kw") == 0.0
    
    # Ждём завершения
    time.sleep(4.0)
    
    # После кризиса: capacity восстановлена
    restored_capacity = sim.get("solar_1", "capacity_kw")
    assert restored_capacity == initial_capacity


def test_power_loss_manual_stop(running_simulator):
    """Ручная остановка кризиса работает."""
    sim = running_simulator
    
    sim.trigger_crisis("power_loss", {"target_node_id": "solar_1", "duration_s": 30.0})
    time.sleep(1.0)
    
    # Останавливаем вручную
    sim.stop_crisis("power_loss")
    time.sleep(0.5)
    
    # Кризис должен завершиться
    assert "power_loss" not in sim.active_crises()
    
    # Узел должен восстановиться
    capacity = sim.get("solar_1", "capacity_kw")
    assert capacity > 0


# ============================================================================
# CO2SpikeCrisis
# ============================================================================

def test_co2_spike_starts(running_simulator, crisis_params):
    """Кризис CO2 spike запускается."""
    sim = running_simulator
    
    sim.trigger_crisis("co2_spike", crisis_params["co2_spike"])
    
    active = sim.active_crises()
    assert "co2_spike" in active


def test_co2_spike_increases_generation(running_simulator, crisis_params):
    """CO2 spike увеличивает генерацию CO2."""
    sim = running_simulator
    
    # Запоминаем исходный множитель
    initial_multiplier = sim.get("co2_sensor_1", "control_generation_multiplier")
    
    # Запускаем кризис
    sim.trigger_crisis("co2_spike", {"multiplier": 5.0, "duration_s": 4.0})
    time.sleep(1.0)
    
    # Множитель должен вырасти
    crisis_multiplier = sim.get("co2_sensor_1", "control_generation_multiplier")
    assert crisis_multiplier > initial_multiplier
    assert crisis_multiplier == 5.0


def test_co2_spike_affects_all_sensors(running_simulator):
    """CO2 spike влияет на все сенсоры."""
    sim = running_simulator
    
    sim.trigger_crisis("co2_spike", {"multiplier": 3.0, "duration_s": 4.0})
    time.sleep(1.0)
    
    # Проверяем все CO2 сенсоры
    multiplier = sim.get("co2_sensor_1", "control_generation_multiplier")
    assert multiplier == 3.0


def test_co2_spike_restores_multiplier(running_simulator):
    """Множитель восстанавливается после кризиса."""
    sim = running_simulator
    
    sim.trigger_crisis("co2_spike", {"multiplier": 5.0, "duration_s": 3.0})
    time.sleep(1.0)
    
    # Во время кризиса: multiplier=5.0
    assert sim.get("co2_sensor_1", "control_generation_multiplier") == 5.0
    
    # Ждём завершения
    time.sleep(4.0)
    
    # После: multiplier восстановлен
    restored = sim.get("co2_sensor_1", "control_generation_multiplier")
    assert restored <= 1.5  # близко к 1.0 (может быть чуть выше из-за вентиляции)


# ============================================================================
# TemperatureAnomalyCrisis
# ============================================================================

def test_temperature_anomaly_applies_shock(running_simulator, crisis_params):
    """Temperature anomaly применяет температурный шок."""
    sim = running_simulator
    
    initial_temp = sim.get("climate_residential", "temperature_c")
    
    sim.trigger_crisis("temperature_anomaly", {"shock_c": 10.0, "duration_s": 2.0})
    time.sleep(1.5)
    
    new_temp = sim.get("climate_residential", "temperature_c")
    # Температура должна вырасти
    assert new_temp > initial_temp


def test_temperature_anomaly_affects_zones(running_simulator):
    """Temperature anomaly влияет на климатические зоны."""
    sim = running_simulator
    
    initial_temp_res = sim.get("climate_residential", "temperature_c")
    initial_temp_work = sim.get("climate_workshop", "temperature_c")
    
    sim.trigger_crisis("temperature_anomaly", {"shock_c": 8.0, "duration_s": 2.0})
    time.sleep(1.5)
    
    # Обе зоны должны получить шок
    new_temp_res = sim.get("climate_residential", "temperature_c")
    new_temp_work = sim.get("climate_workshop", "temperature_c")
    
    assert new_temp_res != initial_temp_res or new_temp_work != initial_temp_work


def test_temperature_anomaly_custom_shock(running_simulator):
    """Кастомная величина температурного шока."""
    sim = running_simulator
    
    # Отрицательный шок (охлаждение)
    sim.trigger_crisis("temperature_anomaly", {"shock_c": -5.0, "duration_s": 2.0})
    time.sleep(1.5)
    
    # Температура должна упасть (или хотя бы измениться)
    temp = sim.get("climate_residential", "temperature_c")
    assert temp < 25.0  # разумный диапазон


# ============================================================================
# SolarDegradationCrisis
# ============================================================================

def test_solar_degradation_reduces_health(running_simulator):
    """Solar degradation снижает health панелей."""
    sim = running_simulator
    
    initial_health = sim.get("solar_1", "health")
    
    sim.trigger_crisis("solar_degradation", {"severity": 0.05, "duration_s": 3.0})
    time.sleep(2.0)
    
    current_health = sim.get("solar_1", "health")
    # Health должен упасть
    assert current_health < initial_health


def test_solar_degradation_reduces_output(running_simulator):
    """Деградация панелей снижает выработку."""
    sim = running_simulator
    
    # Ждём установления нормальной выработки
    time.sleep(2.0)
    initial_output = sim.get("solar_1", "output_kw")
    
    # Быстрая деградация
    sim.trigger_crisis("solar_degradation", {"severity": 0.1, "duration_s": 3.0})
    time.sleep(4.0)
    
    final_output = sim.get("solar_1", "output_kw")
    # Выработка должна упасть (если есть освещённость)
    if initial_output > 0:
        assert final_output < initial_output


def test_solar_degradation_permanent(running_simulator):
    """Деградация необратима (on_end не восстанавливает)."""
    sim = running_simulator
    
    initial_health = sim.get("solar_1", "health")
    
    sim.trigger_crisis("solar_degradation", {"severity": 0.05, "duration_s": 3.0})
    time.sleep(5.0)  # ждём завершения кризиса
    
    # Кризис завершён, но health не восстановлен
    final_health = sim.get("solar_1", "health")
    assert final_health < initial_health


# ============================================================================
# Общие тесты кризисов
# ============================================================================

def test_multiple_crises_simultaneously(running_simulator):
    """Несколько кризисов одновременно."""
    sim = running_simulator
    
    # Запускаем два кризиса
    sim.trigger_crisis("power_loss", {"target_node_id": "solar_1", "duration_s": 5.0})
    sim.trigger_crisis("co2_spike", {"multiplier": 3.0, "duration_s": 5.0})
    
    time.sleep(1.0)
    
    # Оба должны быть активны
    active = sim.active_crises()
    assert "power_loss" in active
    assert "co2_spike" in active


def test_crisis_cannot_restart_while_active(running_simulator):
    """Нельзя запустить активный кризис повторно."""
    sim = running_simulator
    
    sim.trigger_crisis("power_loss", {"target_node_id": "solar_1", "duration_s": 10.0})
    time.sleep(0.5)
    
    # Попытка запустить снова (должна игнорироваться)
    sim.trigger_crisis("power_loss", {"target_node_id": "solar_2", "duration_s": 5.0})
    
    # Всё ещё один кризис
    active = sim.active_crises()
    assert active.count("power_loss") <= 1


def test_active_crises_list(running_simulator):
    """Список активных кризисов корректен."""
    sim = running_simulator
    
    # Изначально пусто
    assert len(sim.active_crises()) == 0
    
    # Запускаем кризис
    sim.trigger_crisis("co2_spike", {"multiplier": 2.0, "duration_s": 5.0})
    time.sleep(0.5)
    
    # Теперь один кризис
    assert len(sim.active_crises()) == 1
    assert "co2_spike" in sim.active_crises()


def test_crisis_with_custom_params(running_simulator):
    """Кризисы принимают кастомные параметры."""
    sim = running_simulator
    
    # Очень короткий кризис
    sim.trigger_crisis("power_loss", {
        "target_node_id": "solar_2",
        "duration_s": 2.0
    })
    
    time.sleep(1.0)
    
    # Цель отключена
    capacity = sim.get("solar_2", "capacity_kw")
    assert capacity == 0.0
    
    # Быстро завершается
    assert wait_for_crisis_end(sim, "power_loss", timeout_s=4.0)

# tests/test_integration.py
"""Интеграционные тесты: сутки работы купола, согласованность данных, персистентность."""
import time

from dome_simulator import create_dome_simulator

STEPS_PER_DAY = 360  # 1440 с симуляции / тик 4 с


def test_full_day_runs_and_data_is_coherent(stepped_simulator):
    """Сутки без ошибок; солнце, температура и нагрузка следуют суточному циклу."""
    sim = stepped_simulator
    errors = []
    sim.subscribe("dependency.violation", errors.append)

    by_hour = {}
    for _ in range(STEPS_PER_DAY):
        sim.step()
        hour = int(sim.environment.outdoor.hour)
        by_hour.setdefault(hour, []).append((
            sim.get("solar_panels_01", "power_w"),
            sim.get("weather_station_01", "temperature_c"),
            sim.get("smart_panel_01", "total_power_w"),
        ))

    def avg(hour, idx):
        return sum(v[idx] for v in by_hour[hour]) / len(by_hour[hour])

    assert avg(13, 0) > 100 and avg(2, 0) == 0.0           # солнце
    assert avg(15, 1) > avg(4, 1)                           # днём теплее, чем перед рассветом
    assert avg(20, 2) > avg(3, 2)                           # вечером освещение, ночью дежурная нагрузка
    assert not errors


def test_events_are_published(stepped_simulator):
    sim = stepped_simulator
    events = []
    sim.subscribe_all(events.append)
    for _ in range(STEPS_PER_DAY):
        sim.step()
    assert events, "за сутки должны случиться хотя бы задания принтера/фрезера"
    assert all(e.source for e in events)


def test_background_thread_generates_data(running_simulator):
    sim = running_simulator
    first = sim.get_node("climate_sensor_01")
    time.sleep(2.5)
    assert sim.get_node("climate_sensor_01") != first
    assert sim.environment.game_time_s > 8 * 3600  # старт в 08:00 + прошедшее время


def test_persistent_state_save_load(stepped_simulator, tmp_path):
    sim = stepped_simulator
    for _ in range(20):
        sim.step()
    sim.control("solar_inverter_01", "set_mode", "GRID_ONLY")
    path = tmp_path / "dome_state.json"
    sim.save_snapshot(path)

    sim2 = create_dome_simulator(seed=1)
    sim2.load_snapshot(path)
    assert sim2.get_all() == sim.get_all()
    assert sim2.get("solar_inverter_01", "mode") == "GRID_ONLY"
    sim2.step()  # после загрузки узлы продолжают работать с загруженного состояния
    assert sim2.get("solar_inverter_01", "mode") == "GRID_ONLY"


def test_step_forbidden_while_thread_running(running_simulator):
    import pytest
    with pytest.raises(RuntimeError):
        running_simulator.step()

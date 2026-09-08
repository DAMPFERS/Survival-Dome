# tests/test_time_control.py
"""Тесты управления временем симуляции."""
import time
import pytest


def test_pause_simulation(running_simulator):
    """Пауза останавливает изменения."""
    sim = running_simulator
    
    # Запоминаем состояние
    initial_charge = sim.get("battery_1", "charge_kwh")
    initial_sim_time = sim.time_controller.sim_time
    
    # Ставим на паузу
    sim.pause()
    
    time.sleep(2.0)
    
    # Состояние не должно измениться
    paused_charge = sim.get("battery_1", "charge_kwh")
    paused_sim_time = sim.time_controller.sim_time
    
    assert abs(paused_charge - initial_charge) < 0.1  # почти не изменился
    assert paused_sim_time == initial_sim_time  # sim_time остановился


def test_resume_simulation(running_simulator):
    """Возобновление продолжает работу."""
    sim = running_simulator
    
    # Ставим на паузу
    sim.pause()
    time.sleep(1.0)
    
    paused_charge = sim.get("battery_1", "charge_kwh")
    
    # Возобновляем
    sim.resume()
    time.sleep(2.0)
    
    # Состояние снова меняется
    resumed_charge = sim.get("battery_1", "charge_kwh")
    assert resumed_charge != paused_charge


def test_time_scale_acceleration(fast_simulator):
    """Ускорение времени (x2, x4)."""
    sim = fast_simulator  # изначально time_scale=4.0
    sim.start()
    
    # При time_scale=4.0 за 2 реальные секунды проходит 8 сек симуляции
    initial_sim_time = sim.time_controller.sim_time
    
    time.sleep(2.0)
    
    final_sim_time = sim.time_controller.sim_time
    elapsed_sim_time = final_sim_time - initial_sim_time
    
    # Должно пройти ~8 сек симуляции (с погрешностью)
    assert 6.0 < elapsed_sim_time < 10.0
    
    sim.stop()


def test_time_scale_change_runtime(running_simulator):
    """Изменение time_scale во время работы."""
    sim = running_simulator
    
    # Изначально time_scale=1.0
    assert sim.time_controller.time_scale == 1.0
    
    # Ускоряем в 2 раза
    sim.set_time_scale(2.0)
    
    assert sim.time_controller.time_scale == 2.0
    
    # Симуляция продолжает работать
    time.sleep(1.0)
    charge = sim.get("battery_1", "charge_kwh")
    assert charge > 0


def test_dt_is_zero_on_pause(running_simulator):
    """dt=0 при паузе."""
    sim = running_simulator
    
    # Запоминаем состояние
    initial_wear = sim.get("structure_1", "wear_pct")
    
    # Ставим на паузу
    sim.pause()
    
    time.sleep(2.0)
    
    # Износ не должен расти (dt=0)
    paused_wear = sim.get("structure_1", "wear_pct")
    assert paused_wear == initial_wear


def test_sim_time_accumulation(fast_simulator):
    """Накопление симуляционного времени."""
    sim = fast_simulator  # time_scale=4.0
    sim.start()
    
    initial_sim_time = sim.time_controller.sim_time
    
    time.sleep(3.0)  # 3 реальные секунды
    
    final_sim_time = sim.time_controller.sim_time
    elapsed = final_sim_time - initial_sim_time
    
    # При time_scale=4 должно пройти ~12 сек симуляции
    assert 10.0 < elapsed < 14.0
    
    sim.stop()

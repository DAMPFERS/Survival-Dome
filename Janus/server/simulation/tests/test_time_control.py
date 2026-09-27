# tests/test_time_control.py
"""Тесты управления временем симуляции."""
import time
import pytest


def test_pause_simulation(running_simulator):
    """Пауза останавливает изменения."""
    sim = running_simulator
    
    # Ставим на паузу и даём завершиться тику, который мог уже идти
    sim.pause()
    time.sleep(1.2)
    initial_state = sim.get_all()
    initial_sim_time = sim.time_controller.sim_time

    time.sleep(2.0)

    # Состояние не должно измениться — при dt=0 узлы не тикают
    assert sim.get_all() == initial_state
    assert sim.time_controller.sim_time == initial_sim_time  # sim_time остановился


def test_resume_simulation(running_simulator):
    """Возобновление продолжает работу."""
    sim = running_simulator
    
    # Ставим на паузу
    sim.pause()
    time.sleep(1.0)
    
    paused_state = sim.get_node("climate_sensor_01")
    paused_game_time = sim.environment.game_time_s

    # Возобновляем
    sim.resume()
    time.sleep(2.5)

    # Состояние снова меняется
    assert sim.environment.game_time_s > paused_game_time
    assert sim.get_node("climate_sensor_01") != paused_state


def test_time_scale_acceleration(fast_simulator):
    """Ускорение времени (x2, x4)."""
    sim = fast_simulator  # изначально time_scale=4.0
    sim.start()
    time.sleep(0.25)  # середина интервала тика: иначе замер попадает на границу тика
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
    soc = sim.get("battery_01", "soc_pct")
    assert soc > 0


def test_dt_is_zero_on_pause(running_simulator):
    """dt=0 при паузе."""
    sim = running_simulator
    
    sim.pause()
    time.sleep(1.2)  # даём завершиться текущему тику
    initial_leak = sim.get("dome_sealing_01", "leak_rate_pct")
    initial_game_time = sim.environment.game_time_s

    time.sleep(2.0)

    # Утечка оболочки не растёт, игровое время стоит (dt=0)
    assert sim.get("dome_sealing_01", "leak_rate_pct") == initial_leak
    assert sim.environment.game_time_s == initial_game_time


def test_sim_time_accumulation(fast_simulator):
    """Накопление симуляционного времени."""
    sim = fast_simulator  # time_scale=4.0
    sim.start()
    time.sleep(0.25)  # середина интервала тика: иначе замер попадает на границу тика
    initial_sim_time = sim.time_controller.sim_time
    
    time.sleep(3.0)  # 3 реальные секунды
    
    final_sim_time = sim.time_controller.sim_time
    elapsed = final_sim_time - initial_sim_time
    
    # При time_scale=4 должно пройти ~12 сек симуляции
    assert 10.0 < elapsed < 14.0
    
    sim.stop()

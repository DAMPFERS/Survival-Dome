# tests/conftest.py
"""Фикстуры pytest для тестирования симулятора купола."""
import sys
import time
from pathlib import Path

import pytest

# Добавляем родительскую директорию в PYTHONPATH
parent_dir = Path(__file__).parent.parent
sys.path.insert(0, str(parent_dir))

from dome_simulator import create_dome_simulator


@pytest.fixture
def simulator():
    """Базовый симулятор с коротким tick_interval для быстрых тестов."""
    sim = create_dome_simulator(tick_interval=1.0, time_scale=1.0)
    yield sim
    if sim.is_running:
        sim.stop()


@pytest.fixture
def fast_simulator():
    """Ускоренный симулятор для длинных тестов (x4 скорость)."""
    sim = create_dome_simulator(tick_interval=0.5, time_scale=4.0)
    yield sim
    if sim.is_running:
        sim.stop()


@pytest.fixture
def running_simulator(simulator):
    """Уже запущенный симулятор, готовый к использованию."""
    simulator.start()
    time.sleep(0.5)  # даём потоку стартовать
    yield simulator
    simulator.stop()


@pytest.fixture
def event_collector():
    """Коллектор событий для проверки публикации событий."""
    events = []
    
    def collector(event):
        events.append(event)
    
    collector.events = events
    collector.clear = lambda: events.clear()
    return collector


@pytest.fixture
def crisis_params():
    """Базовые параметры для тестирования кризисов."""
    return {
        "power_loss": {"target_node_id": "solar_1", "duration_s": 8.0},
        "co2_spike": {"multiplier": 5.0, "duration_s": 8.0},
        "temperature_anomaly": {"shock_c": 10.0, "duration_s": 8.0},
        "solar_degradation": {"severity": 0.01, "duration_s": 8.0}
    }

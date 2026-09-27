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
def stepped_simulator():
    """Симулятор без фонового потока с фиксированным seed — для детерминированных
    юнит-тестов узлов через sim.step(). Прежняя модель времени (legacy): один шаг =
    4 с симуляции = 4 игровые минуты; правила зависимостей выключены — узлы независимы."""
    return create_dome_simulator(tick_interval=4.0, time_scale=1.0, seed=12345, time_model="legacy", rules=False)


@pytest.fixture
def dome():
    """Полный купол по умолчанию: гибридное время (сутки 3 ч, физика 1:1),
    правила зависимостей, автоматика и каталог кризисов. Шаг 4 с."""
    return create_dome_simulator(tick_interval=4.0, time_scale=1.0, seed=777)


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


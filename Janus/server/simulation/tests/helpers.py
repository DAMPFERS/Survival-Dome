# tests/helpers.py
"""Вспомогательные утилиты для тестирования симулятора."""
import time
from typing import Callable, Optional


def wait_for_condition(sim, condition_fn: Callable, timeout_s: float = 10, 
                       check_interval_s: float = 0.5) -> bool:
    """
    Ожидает выполнения условия с таймаутом.
    
    Args:
        sim: Инстанс симулятора
        condition_fn: Функция, возвращающая True при выполнении условия
        timeout_s: Максимальное время ожидания
        check_interval_s: Интервал проверки условия
        
    Returns:
        True если условие выполнилось, False если таймаут
    """
    start_time = time.time()
    while time.time() - start_time < timeout_s:
        if condition_fn(sim):
            return True
        time.sleep(check_interval_s)
    return False


def assert_event_published(event_collector, event_type: str, timeout_s: float = 5) -> bool:
    """
    Проверяет, что событие определённого типа было опубликовано.
    
    Args:
        event_collector: Коллектор событий из фикстуры
        event_type: Тип события для поиска
        timeout_s: Таймаут ожидания события
        
    Returns:
        True если событие найдено
    """
    start_time = time.time()
    while time.time() - start_time < timeout_s:
        for event in event_collector.events:
            if event.type == event_type:
                return True
        time.sleep(0.1)
    return False


def simulate_for_seconds(sim, sim_seconds: float) -> None:
    """
    Прогоняет симуляцию на заданное количество симуляционных секунд.
    
    Args:
        sim: Инстанс симулятора
        sim_seconds: Количество симуляционных секунд
    """
    time_scale = sim.time_controller.time_scale
    tick_interval = sim.time_controller.tick_interval
    
    # Вычисляем реальное время для симуляции
    real_seconds = sim_seconds / time_scale if time_scale > 0 else 0
    
    if real_seconds > 0:
        time.sleep(real_seconds + tick_interval * 0.5)  # +0.5 тика на запас


def simulate_for_ticks(sim, tick_count: int) -> None:
    """
    Прогоняет симуляцию на заданное количество тиков.
    
    Args:
        sim: Инстанс симулятора
        tick_count: Количество тиков
    """
    tick_interval = sim.time_controller.tick_interval
    real_seconds = tick_interval * tick_count
    time.sleep(real_seconds + tick_interval * 0.5)


def get_event_count(event_collector, event_type: Optional[str] = None, 
                   severity: Optional[str] = None) -> int:
    """
    Считает количество событий определённого типа и/или уровня серьёзности.
    
    Args:
        event_collector: Коллектор событий
        event_type: Тип события (None = все типы)
        severity: Уровень серьёзности (None = все уровни)
        
    Returns:
        Количество подходящих событий
    """
    count = 0
    for event in event_collector.events:
        type_match = event_type is None or event.type == event_type
        severity_match = severity is None or event.severity.value == severity
        if type_match and severity_match:
            count += 1
    return count


def get_events_by_type(event_collector, event_type: str) -> list:
    """Возвращает все события определённого типа."""
    return [e for e in event_collector.events if e.type == event_type]


def wait_for_crisis_end(sim, crisis_name: str, timeout_s: float = 20) -> bool:
    """
    Ожидает завершения кризиса.
    
    Args:
        sim: Инстанс симулятора
        crisis_name: Название кризиса
        timeout_s: Таймаут ожидания
        
    Returns:
        True если кризис завершился, False если таймаут
    """
    def crisis_ended(s):
        return crisis_name not in s.active_crises()
    
    return wait_for_condition(sim, crisis_ended, timeout_s=timeout_s, check_interval_s=0.2)


def assert_parameter_in_range(sim, node_id: str, param: str, 
                              min_val: float, max_val: float) -> bool:
    """
    Проверяет, что параметр узла находится в заданном диапазоне.
    
    Args:
        sim: Инстанс симулятора
        node_id: ID узла
        param: Название параметра
        min_val: Минимальное значение
        max_val: Максимальное значение
        
    Returns:
        True если значение в диапазоне
    """
    value = sim.get(node_id, param)
    return min_val <= value <= max_val


def collect_parameter_over_time(sim, node_id: str, param: str, 
                                duration_s: float, interval_s: float = 0.5) -> list:
    """
    Собирает значения параметра за период времени.
    
    Args:
        sim: Инстанс симулятора
        node_id: ID узла
        param: Название параметра
        duration_s: Длительность сбора
        interval_s: Интервал между замерами
        
    Returns:
        Список кортежей (timestamp, value)
    """
    values = []
    start_time = time.time()
    
    while time.time() - start_time < duration_s:
        current_time = time.time() - start_time
        value = sim.get(node_id, param)
        values.append((current_time, value))
        time.sleep(interval_s)
    
    return values

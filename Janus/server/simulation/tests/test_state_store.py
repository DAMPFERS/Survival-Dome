# tests/test_state_store.py
"""Тесты операций чтения/записи параметров через StateStore."""
import time
import pytest
from dome_simulator.store import NodeNotFoundError


def test_read_parameter_from_node(simulator):
    """Чтение одного параметра из узла."""
    charge_pct = simulator.get("battery_1", "charge_pct")
    assert isinstance(charge_pct, (int, float))
    assert 0 <= charge_pct <= 1.0


def test_read_all_node_parameters(simulator):
    """Чтение всех параметров конкретного узла."""
    node_state = simulator.get_node("solar_1")
    
    assert isinstance(node_state, dict)
    assert "capacity_kw" in node_state
    assert "output_kw" in node_state
    assert "health" in node_state
    assert node_state["capacity_kw"] == 12.0


def test_read_all_nodes(simulator):
    """Чтение всех узлов системы."""
    all_nodes = simulator.get_all()
    
    assert isinstance(all_nodes, dict)
    assert len(all_nodes) > 20  # должно быть много узлов
    assert "battery_1" in all_nodes
    assert "solar_1" in all_nodes
    assert "cnc_1" in all_nodes


def test_set_parameter_and_verify(simulator):
    """Установка параметра и проверка изменения."""
    # Устанавливаем новое значение
    simulator.set("battery_1", "charge_pct", 0.5)
    
    # Проверяем изменение
    new_value = simulator.get("battery_1", "charge_pct")
    assert new_value == 0.5


def test_update_multiple_parameters(simulator):
    """Обновление нескольких параметров одновременно."""
    # Обновляем сразу несколько параметров
    simulator.update("diesel_1", running=True, fuel_l=150.0)
    
    # Проверяем оба параметра
    assert simulator.get("diesel_1", "running") == True
    assert simulator.get("diesel_1", "fuel_l") == 150.0


def test_parameter_persistence_across_ticks(running_simulator):
    """Параметр сохраняется между тиками."""
    sim = running_simulator
    
    # Устанавливаем значение
    sim.set("battery_1", "charge_kwh", 25.0)
    initial_value = sim.get("battery_1", "charge_kwh")
    
    # Ждём несколько тиков
    time.sleep(3.0)
    
    # Значение должно измениться (саморазряд), но не сброситься
    current_value = sim.get("battery_1", "charge_kwh")
    assert current_value != initial_value  # изменилось
    assert abs(current_value - initial_value) < 1.0  # но не сильно


def test_read_nonexistent_node(simulator):
    """Чтение несуществующего узла вызывает ошибку."""
    with pytest.raises(NodeNotFoundError):
        simulator.get("nonexistent_node", "some_param")


def test_read_nonexistent_parameter(simulator):
    """Чтение несуществующего параметра возвращает default."""
    value = simulator.get("battery_1", "nonexistent_param", default=999)
    assert value == 999


def test_snapshot_consistency(simulator):
    """Snapshot содержит все необходимые данные."""
    snapshot = simulator.snapshot()
    
    # Проверяем структуру
    assert "timestamp" in snapshot
    assert "nodes" in snapshot
    assert isinstance(snapshot["timestamp"], (int, float))
    assert isinstance(snapshot["nodes"], dict)
    
    # Проверяем содержимое узлов
    assert "battery_1" in snapshot["nodes"]
    assert "solar_1" in snapshot["nodes"]
    
    # Snapshot должен содержать все параметры узла
    battery_state = snapshot["nodes"]["battery_1"]
    assert "charge_kwh" in battery_state
    assert "charge_pct" in battery_state


def test_concurrent_reads(running_simulator):
    """Параллельное чтение из разных потоков (потокобезопасность)."""
    import threading
    
    sim = running_simulator
    results = []
    errors = []
    
    def read_multiple_times():
        try:
            for _ in range(10):
                value = sim.get("battery_1", "charge_pct")
                results.append(value)
                time.sleep(0.01)
        except Exception as e:
            errors.append(e)
    
    # Запускаем 5 потоков одновременно
    threads = [threading.Thread(target=read_multiple_times) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    # Проверяем: ошибок нет, все чтения успешны
    assert len(errors) == 0
    assert len(results) == 50  # 10 чтений * 5 потоков
    assert all(isinstance(v, (int, float)) for v in results)

# tests/test_state_store.py
"""Тесты операций чтения/записи параметров через StateStore."""
import time
import pytest
from dome_simulator.store import NodeNotFoundError


def test_read_parameter_from_node(simulator):
    """Чтение одного параметра из узла."""
    soc_pct = simulator.get("battery_01", "soc_pct")
    assert isinstance(soc_pct, (int, float))
    assert 0 <= soc_pct <= 100.0


def test_read_all_node_parameters(simulator):
    """Чтение всех параметров конкретного узла."""
    node_state = simulator.get_node("solar_panels_01")

    assert isinstance(node_state, dict)
    assert "power_w" in node_state
    assert "pv_voltage_v" in node_state
    assert "active_mppt_count" in node_state
    assert node_state["channels_count"] == 2


def test_read_all_nodes(simulator):
    """Чтение всех узлов системы."""
    all_nodes = simulator.get_all()

    assert isinstance(all_nodes, dict)
    assert len(all_nodes) > 20  # должно быть много узлов
    assert "battery_01" in all_nodes
    assert "solar_panels_01" in all_nodes
    assert "cnc_01" in all_nodes


def test_set_parameter_and_verify(simulator):
    """Установка параметра и проверка изменения."""
    simulator.set("fuel_tank_01", "reserve_l", 50.0)
    assert simulator.get("fuel_tank_01", "reserve_l") == 50.0


def test_update_multiple_parameters(simulator):
    """Обновление нескольких параметров одновременно."""
    simulator.update("dizel_1", control_load_w=1500.0, control_fuel_available=False)

    assert simulator.get("dizel_1", "control_load_w") == 1500.0
    assert simulator.get("dizel_1", "control_fuel_available") is False


def test_returned_state_is_a_copy(simulator):
    """Вложенные структуры отдаются копией: изменение снаружи не портит хранилище."""
    lines = simulator.get("smart_panel_01", "lines")
    lines[0]["state"] = "BROKEN"
    assert simulator.get("smart_panel_01", "lines")[0]["state"] == "ON"


def test_parameter_persistence_across_ticks(running_simulator):
    """Входной параметр сохраняется между тиками и учитывается узлом."""
    sim = running_simulator

    sim.set("smart_panel_01", "control_phantom_load_w", 700.0)
    time.sleep(3.0)

    # Значение не сбросилось, а узел его учёл (фантомная нагрузка на линии 8)
    assert sim.get("smart_panel_01", "control_phantom_load_w") == 700.0
    assert sim.get("smart_panel_01", "lines")[7]["power_w"] >= 700.0


def test_read_nonexistent_node(simulator):
    """Чтение несуществующего узла вызывает ошибку."""
    with pytest.raises(NodeNotFoundError):
        simulator.get("nonexistent_node", "some_param")


def test_read_nonexistent_parameter(simulator):
    """Чтение несуществующего параметра возвращает default."""
    value = simulator.get("battery_01", "nonexistent_param", default=999)
    assert value == 999


def test_snapshot_consistency(simulator):
    """Snapshot содержит все необходимые данные."""
    snapshot = simulator.snapshot()

    assert "timestamp" in snapshot
    assert "nodes" in snapshot
    assert isinstance(snapshot["timestamp"], (int, float))
    assert isinstance(snapshot["nodes"], dict)

    assert "battery_01" in snapshot["nodes"]
    assert "solar_panels_01" in snapshot["nodes"]

    battery_state = snapshot["nodes"]["battery_01"]
    assert "soc_pct" in battery_state
    assert "voltage_v" in battery_state


def test_concurrent_reads(running_simulator):
    """Параллельное чтение из разных потоков (потокобезопасность)."""
    import threading

    sim = running_simulator
    results = []
    errors = []

    def read_multiple_times():
        try:
            for _ in range(10):
                results.append(sim.get("battery_01", "soc_pct"))
                time.sleep(0.01)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=read_multiple_times) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(results) == 50  # 10 чтений * 5 потоков
    assert all(isinstance(v, (int, float)) for v in results)

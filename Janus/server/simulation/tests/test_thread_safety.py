# tests/test_thread_safety.py
"""Тесты потокобезопасности симулятора."""
import time
import threading
import pytest


def test_concurrent_reads_from_multiple_threads(running_simulator):
    """Параллельное чтение из нескольких потоков."""
    sim = running_simulator
    
    results = []
    errors = []
    
    def read_worker(thread_id):
        try:
            for i in range(20):
                charge = sim.get("battery_1", "charge_pct")
                output = sim.get("solar_1", "output_kw")
                results.append((thread_id, i, charge, output))
                time.sleep(0.01)
        except Exception as e:
            errors.append((thread_id, e))
    
    # Запускаем 10 потоков
    threads = [threading.Thread(target=read_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    # Ошибок быть не должно
    assert len(errors) == 0
    assert len(results) == 200  # 20 чтений * 10 потоков


def test_concurrent_writes_from_api(running_simulator):
    """Параллельная запись через API."""
    sim = running_simulator
    
    errors = []
    
    def write_worker(thread_id):
        try:
            for i in range(10):
                # Каждый поток пишет своё уникальное значение
                value = thread_id * 100 + i
                sim.set("diesel_1", "fuel_l", float(value))
                time.sleep(0.02)
        except Exception as e:
            errors.append((thread_id, e))
    
    # Запускаем 5 потоков пишущих в один параметр
    threads = [threading.Thread(target=write_worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    # Не должно быть ошибок (даже если значения перезаписываются)
    assert len(errors) == 0
    
    # Финальное значение должно быть валидным
    final_fuel = sim.get("diesel_1", "fuel_l")
    assert 0 <= final_fuel <= 500


def test_control_during_tick(running_simulator):
    """Управление узлом во время выполнения тика."""
    sim = running_simulator
    
    errors = []
    
    def control_worker():
        try:
            for _ in range(15):
                sim.control("cnc_1", "start_job", job_name="test", duration_s=2.0)
                time.sleep(0.3)
                sim.control("cnc_1", "cancel_job")
                time.sleep(0.2)
        except Exception as e:
            errors.append(e)
    
    # Запускаем контроль в отдельном потоке
    thread = threading.Thread(target=control_worker)
    thread.start()
    thread.join()
    
    # Не должно быть гонок и дедлоков
    assert len(errors) == 0


def test_snapshot_during_simulation(running_simulator):
    """Snapshot во время активной работы симулятора."""
    sim = running_simulator
    
    snapshots = []
    errors = []
    
    def snapshot_worker():
        try:
            for _ in range(10):
                snap = sim.snapshot()
                snapshots.append(snap)
                time.sleep(0.2)
        except Exception as e:
            errors.append(e)
    
    # Делаем снапшоты в отдельном потоке
    thread = threading.Thread(target=snapshot_worker)
    thread.start()
    thread.join()
    
    # Все снапшоты должны быть корректны
    assert len(errors) == 0
    assert len(snapshots) == 10
    
    for snap in snapshots:
        assert "timestamp" in snap
        assert "nodes" in snap
        assert "battery_1" in snap["nodes"]

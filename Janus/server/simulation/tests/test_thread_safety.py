# tests/test_thread_safety.py
"""Тесты потокобезопасности симулятора."""
import time
import threading


def test_concurrent_reads_from_multiple_threads(running_simulator):
    """Параллельное чтение из нескольких потоков."""
    sim = running_simulator

    results = []
    errors = []

    def read_worker(thread_id):
        try:
            for i in range(20):
                soc = sim.get("battery_01", "soc_pct")
                power = sim.get("solar_panels_01", "power_w")
                results.append((thread_id, i, soc, power))
                time.sleep(0.01)
        except Exception as e:
            errors.append((thread_id, e))

    threads = [threading.Thread(target=read_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(results) == 200  # 20 чтений * 10 потоков


def test_concurrent_writes_from_api(running_simulator):
    """Параллельная запись через API."""
    sim = running_simulator

    errors = []

    def write_worker(thread_id):
        try:
            for i in range(10):
                sim.set("dizel_1", "control_load_w", float(thread_id * 100 + i))
                time.sleep(0.02)
        except Exception as e:
            errors.append((thread_id, e))

    threads = [threading.Thread(target=write_worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert 0 <= sim.get("dizel_1", "control_load_w") <= 500


def test_control_during_tick(running_simulator):
    """Управление узлом во время выполнения тика."""
    sim = running_simulator

    errors = []

    def control_worker():
        try:
            for _ in range(15):
                if sim.get("cnc_01", "state") == "ALARM":  # случайная авария за время теста
                    sim.control("cnc_01", "reset_alarm")
                if sim.get("cnc_01", "state") == "IDLE":
                    sim.control("cnc_01", "start_job", job_name="test", duration_s=3600.0)
                time.sleep(0.3)
                if sim.get("cnc_01", "state") in ("RUNNING", "PAUSED"):
                    sim.control("cnc_01", "abort")
                time.sleep(0.2)
        except Exception as e:
            errors.append(e)

    thread = threading.Thread(target=control_worker)
    thread.start()
    thread.join()

    # Не должно быть гонок и дедлоков
    assert len(errors) == 0


def test_control_not_lost_during_ticks(running_simulator):
    """Команда, пришедшая во время тика, не перетирается результатами тика."""
    sim = running_simulator
    for i in range(20):
        mode = "GRID_ONLY" if i % 2 else "HYBRID"
        sim.control("solar_inverter_01", "set_mode", mode)
        time.sleep(0.07)
        assert sim.get("solar_inverter_01", "mode") == mode


def test_snapshot_during_simulation(running_simulator):
    """Snapshot во время активной работы симулятора."""
    sim = running_simulator

    snapshots = []
    errors = []

    def snapshot_worker():
        try:
            for _ in range(10):
                snapshots.append(sim.snapshot())
                time.sleep(0.2)
        except Exception as e:
            errors.append(e)

    thread = threading.Thread(target=snapshot_worker)
    thread.start()
    thread.join()

    assert len(errors) == 0
    assert len(snapshots) == 10

    for snap in snapshots:
        assert "timestamp" in snap
        assert "nodes" in snap
        assert "battery_01" in snap["nodes"]

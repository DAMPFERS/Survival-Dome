# tests/test_shift.py
"""Сценарий 90-минутной смены (dome_crises.md, раздел 8) и отчёт для оценки (раздел 9)."""
import pytest

from dome_simulator import create_dome_simulator
from reference_agent import ReferenceAgent, run_shift


@pytest.fixture(scope="module")
def good_report():
    return run_shift(create_dome_simulator(seed=3), ReferenceAgent)


@pytest.fixture(scope="module")
def idle_report():
    return run_shift(create_dome_simulator(seed=3))


def test_timeline_fully_played(good_report):
    codes = [c["code"] for c in good_report["crises"]]
    for code in ("№37", "№22", "К13", "№16", "№1", "№5", "№19", "№6", "№26", "№23", "№29", "№2",
                 "К1", "№17", "№27", "К4", "№31", "№47", "К3"):
        assert code in codes, code
    assert good_report["finished"] and good_report["elapsed_s"] >= 90 * 60


def test_reference_agent_resolves_shift(good_report):
    outcomes = {c["code"]: c["outcome"] for c in good_report["crises"]}
    assert outcomes.pop("№19") == "MANUAL"
    assert set(outcomes.values()) == {"RESOLVED"}, outcomes
    m = good_report["metrics"]
    assert m["blackouts"] == 0 and m["co2_over_1500_s"] == 0.0
    assert m["cnc_downtime_s"] < 300 and m["printer_downtime_s"] < 120
    assert m["fire_suppression_starts"] == [s for s in m["fire_suppression_starts"] if "STORAGE" in s]
    assert not good_report["penalties"]
    assert good_report["operator_physical_actions"]  # просьбы к Оператору учтены


def test_idle_shift_fails_most_crises(idle_report):
    outcomes = [c["outcome"] for c in idle_report["crises"]]
    assert outcomes.count("FAILED") >= 8
    m = idle_report["metrics"]
    assert m["cnc_downtime_s"] > 1800
    assert any("FABLAB" in s for s in m["fire_suppression_starts"])   # ложная тревога залила FabLab


def test_equal_conditions_for_teams():
    """Одинаковый сценарий для всех команд: тот же старт независимо от предыстории симулятора."""
    a = create_dome_simulator(seed=3)
    b = create_dome_simulator(seed=99)
    for _ in range(50):
        b.step()                       # у второй команды симулятор уже поработал
    a.start_shift()
    b.start_shift()
    for _ in range(300):
        a.step()
        b.step()
    assert a.get_all() == b.get_all()
    assert a.environment_snapshot()["outdoor"] == b.environment_snapshot()["outdoor"]


def test_shift_start_conditions():
    sim = create_dome_simulator(seed=3)
    sim.start_shift()
    assert sim.environment.outdoor.hour == pytest.approx(13.5)
    assert sim.get("fuel_tank_01", "fuel_quality") == "WATER"
    assert sim.shift is not None and not sim.shift.finished
    sim.step()
    report = sim.shift_report()
    assert {c["code"] for c in report["crises"]} >= {"№37", "№22", "К13"}

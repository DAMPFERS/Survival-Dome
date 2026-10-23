# tests/test_real_cnc.py
"""Адаптер реального фрезера (real_devices.CNCAdapter) с подставным контроллером GRBL:
какие строки уходят на станок для move / set_zero / probe_z и когда команды отклоняются."""
import asyncio
import sys
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVER_DIR))

from real_devices import CNCAdapter, RealCommandError  # noqa: E402


class FakeController:
    def __init__(self, machine_state="Idle", milling=False):
        self.state = SimpleNamespace(machine_state=machine_state, milling=milling, error=None, online=True)
        self.lines: list[str] = []
        self.probes: list[dict] = []

    def get_state(self):
        return self.state

    @staticmethod
    def _done(result="ok") -> Future:
        fut = Future()
        fut.set_result(result)
        return fut

    def send_command(self, line):
        self.lines.append(line)
        return self._done()

    def probe_z_zero(self, **params):
        self.probes.append(params)
        return self._done()


def run(adapter, ctl, action, value=None):
    asyncio.run(asyncio.to_thread(adapter._execute_sync, ctl, action, value))


@pytest.fixture
def adapter():
    return CNCAdapter("127.0.0.1", gcode_dir=SERVER_DIR / "frezer" / "gcode")


def test_move_is_jog_and_keeps_modal_state(adapter):
    ctl = FakeController()
    run(adapter, ctl, "move", {"x": 10, "z": -2.5, "feedrate": 800})
    run(adapter, ctl, "move", {"x": 0, "y": 0, "mode": "absolute"})
    assert ctl.lines == ["$J=G91 G21 X10.000 Z-2.500 F800", "$J=G90 G21 X0.000 Y0.000 F1000"]


def test_set_zero_active_coordinate_system(adapter):
    ctl = FakeController()
    run(adapter, ctl, "set_zero", "xy")
    run(adapter, ctl, "set_zero")
    assert ctl.lines == ["G10 L20 P0 X0 Y0", "G10 L20 P0 X0 Y0 Z0"]


def test_probe_passes_only_given_params(adapter):
    ctl = FakeController()
    run(adapter, ctl, "probe_z", {"plate_thickness": 0.5, "retract": 3})
    run(adapter, ctl, "probe_z")
    assert ctl.probes == [{"plate_thickness": 0.5, "retract": 3.0}, {}]


@pytest.mark.parametrize("state, milling, action", [
    ("Run", True, "move"),           # идёт задание
    ("Hold:0", True, "set_zero"),    # задание на паузе
    ("Alarm", False, "probe_z"),
    ("Jog", False, "set_zero"),      # ноль — только после остановки
])
def test_setup_rejected_outside_idle(adapter, state, milling, action):
    ctl = FakeController(state, milling)
    with pytest.raises(RealCommandError):
        run(adapter, ctl, action, {"x": 1} if action == "move" else None)
    assert ctl.lines == [] and ctl.probes == []


def test_jog_allowed_while_jogging(adapter):
    ctl = FakeController("Jog")
    run(adapter, ctl, "move", {"y": 5})
    assert ctl.lines == ["$J=G91 G21 Y5.000 F1000"]


def test_bad_params_same_message_as_simulator(adapter):
    ctl = FakeController()
    with pytest.raises(RealCommandError, match="неизвестные параметры"):
        run(adapter, ctl, "probe_z", {"foo": 1})
    with pytest.raises(RealCommandError, match="вне диапазона"):
        run(adapter, ctl, "move", {"x": 400})
    assert ctl.lines == [] and ctl.probes == []

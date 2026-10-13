# tests/test_holds.py
"""
Удержание станков сервером, ALARM, деталь на столе принтера, заморозка линий щитка,
тихий дроп команд (holds.py) и кризисы бокса ФабЛаб №50–51.
"""
from __future__ import annotations

import pytest

from dome_simulator import ControlError, holds
from test_crises import A, AUTO, OP, at, make, play

PRINTER, CNC, PANEL, FAB = "printer_3d_01", "cnc_01", "smart_panel_01", "thermal_insulation_01"


def trip_co2(sim):
    sim.trigger_crisis("6")
    sim.run_for(80)
    assert sim.get(PRINTER, "hold_reason") == "IL_CO2" and sim.get(CNC, "hold_reason") == "IL_CO2"


def test_interlock_hold_and_blind_resumes_lead_to_alarm():
    sim = make()
    trip_co2(sim)
    for node in (PRINTER, CNC):
        assert sim.get(node, "state") == "PAUSED" and sim.get(node, "hold_level") == "WARNING"
    assert sim.get(CNC, "spindle_target_rpm") == 0.0             # пауза со стопом шпинделя

    for attempt in (1, 2):
        sim.control(PRINTER, "resume", origin=A)                  # API: успех, станок стоит
        assert sim.get(PRINTER, "state") == "PAUSED" and sim.get(PRINTER, "resume_attempts") == attempt
    sim.control(PRINTER, "resume", origin=A)
    p = sim.get_node(PRINTER)
    assert p["hold_level"] == "ALARM" and p["state"] == "IDLE" and p["file_name"] is None   # задание отменено
    assert p["lockout_remaining_s"] == holds.LOCKOUT_S
    with pytest.raises(ControlError, match="ALARM"):
        sim.control(PRINTER, "start_job", {"file_name": "x.gcode", "duration_s": 60}, origin=A)

    for _ in range(3):
        sim.control(CNC, "resume", origin=A)
    c = sim.get_node(CNC)
    assert c["hold_level"] == "ALARM" and c["state"] == "PAUSED" and c["file_name"]   # фрезер ждёт

    sim.run_for(holds.LOCKOUT_S + 10)                             # кризис кончился, CO₂ в норме
    assert sim.get(PRINTER, "hold_level") is None
    c = sim.get_node(CNC)
    assert c["hold_level"] == "WARNING" and c["resume_attempts"] == 0
    sim.control(CNC, "resume", origin=A)
    assert sim.get(CNC, "state") == "RUNNING" and sim.get(CNC, "hold_level") is None


def test_resume_after_fixing_cause():
    sim = make()
    trip_co2(sim)
    sim.control(AUTO, "set_data_trust", {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"},
                origin=A)
    sim.control(AUTO, "reset_interlock", "IL_CO2", origin=A)
    sim.step()
    assert not holds.cause_active(sim.ctx, "IL_CO2")
    sim.control(PRINTER, "resume", origin=A)
    assert sim.get(PRINTER, "state") == "PRINTING" and sim.get(PRINTER, "hold_level") is None
    assert sim.get(PRINTER, "resume_attempts") == 0


def test_admin_resume_clears_hold():
    sim = make()
    trip_co2(sim)
    sim.control(PRINTER, "resume")                                 # админ: без проверки причины
    sim.run_for(12)
    assert sim.get(PRINTER, "state") == "PRINTING" and sim.get(PRINTER, "hold_level") is None
    sim.control(CNC, "release_hold")
    assert sim.get(CNC, "hold_level") is None and sim.get(CNC, "state") == "PAUSED"


def finish_print(sim):
    sim.control(PRINTER, "start_job", {"file_name": "part.gcode", "duration_s": 30}, origin=A)
    for _ in range(200):
        sim.step()
        if sim.get(PRINTER, "state") == "COMPLETED":
            break
    sim.step()
    assert sim.get(PRINTER, "state") == "COMPLETED" and sim.get(PRINTER, "bed_clear") is False


def test_bed_collision_and_part_removal():
    sim = make(jobs=False)
    finish_print(sim)
    sim.control(PRINTER, "start_job", {"file_name": "next.gcode", "duration_s": 60}, origin=A)
    p = sim.get_node(PRINTER)
    assert p["hold_level"] == "ALARM" and p["hold_reason"] == "BED_COLLISION" and p["state"] == "COMPLETED"

    sim.control(PRINTER, "release_hold")
    with pytest.raises(ControlError, match="заблокирована"):
        sim.control(FAB, "set_door", {"door": 0, "open": True}, origin=OP)   # замок закрыт
    sim.control(FAB, "set_lock", {"lock": 0, "locked": False}, origin=A)
    sim.control(FAB, "set_door", {"door": 0, "open": True}, origin=OP)
    sim.step()
    assert sim.get(PRINTER, "bed_clear") is True                   # Оператор забрал деталь
    sim.control(PRINTER, "start_job", {"file_name": "next.gcode", "duration_s": 60}, origin=A)
    assert sim.get(PRINTER, "state") == "PRINTING"


def test_line_chatter_lockout():
    sim = make(jobs=False)
    for action in ("line_off", "line_on", "line_off"):
        sim.control(PANEL, action, 8, origin=A)
    with pytest.raises(ControlError, match="дребезг"):
        sim.control(PANEL, "line_on", 8, origin=A)
    line = sim.get(PANEL, "lines")[7]
    assert line["state"] == "OFF" and line["lockout_reason"] == "CHATTER" and line["lockout_remaining_s"] > 0
    assert not sim.ctx.command(PANEL, "line_on", 8, origin="automation")   # реле не реагирует и на автоматику
    sim.run_for(holds.LINE_CHATTER_LOCKOUT_S + 8)
    sim.control(PANEL, "line_on", 8, origin=A)
    assert sim.get(PANEL, "lines")[7]["state"] == "ON"


def test_silent_drop_n24():
    sim = make(jobs=False)
    report = play(sim, "24", {"line": 8, "actions": ["line_off"]},
                  [at(8, PANEL, "line_off", 8), at(40, PANEL, "line_off", 8)], 80)
    assert report.outcome == "RESOLVED", report.notes
    sim = make(jobs=False)
    sim.trigger_crisis("24", {"line": 8, "actions": ["line_off"]})
    sim.control(PANEL, "line_off", 8, origin=A)                    # успех в API
    assert sim.get(PANEL, "lines")[7]["state"] == "ON"             # но реле не переключилось
    sim.stop_crisis("24")
    assert sim.scenario_engine.reports()[-1]["outcome"] == "FAILED"


def test_mstyuk_bus_collision_drops_writes():
    sim = make(jobs=False)
    sim.trigger_crisis("22")
    sim.control(FAB, "set_lock", {"lock": 0, "locked": False}, origin=A)
    assert sim.get(FAB, "locks")[0] == "LOCKED"                    # кадр потерян, ошибки нет
    sim.control(FAB, "set_poll_interval", 500, origin=A)
    sim.control(FAB, "set_lock", {"lock": 0, "locked": False}, origin=A)
    assert sim.get(FAB, "locks")[0] == "UNLOCKED"


def test_n50_fablab_voc():
    sim = make()
    bad = play(sim, "50", {}, [], 900)
    assert bad.outcome == "FAILED", bad.notes
    assert sim.get(PRINTER, "hold_reason") in ("IL_VOC_BOX", None)
    sim = make()
    fans = [at(20, FAB, "set_fan_pwm", {"fan": f, "pwm": 255}) for f in (0, 1, 2, 3, 5)]
    good = play(sim, "50", {}, fans, 300)
    assert good.outcome == "RESOLVED", good.notes
    assert sim.get(FAB, "fault_mode") == 0                          # неисправность стенда снята


def test_n50_blind_resume_vs_fix():
    sim = make()
    sim.trigger_crisis("50")
    sim.run_for(240)
    assert sim.get(PRINTER, "hold_reason") == "IL_VOC_BOX"
    sim.control(PRINTER, "resume", origin=A)
    assert sim.get(PRINTER, "state") == "PAUSED"                    # VOC высокий — пустая попытка
    for f in (0, 1, 2, 3, 5):
        sim.control(FAB, "set_fan_pwm", {"fan": f, "pwm": 255}, origin=A)
    sim.run_for(240)
    sim.control(PRINTER, "resume", origin=A)
    assert sim.get(PRINTER, "state") == "PRINTING"


def cooled_part(sim):
    finish_print(sim)
    for _ in range(150):
        sim.step()
        if sim.get(PRINTER, "bed_temp_c") < 40:
            break


def test_n51_lock_trap():
    sim = make(jobs=False)
    cooled_part(sim)
    bad = play(sim, "51", {}, [], 640)
    assert bad.outcome == "FAILED"

    sim = make(jobs=False)
    cooled_part(sim)
    sim.trigger_crisis("51")
    sim.step()
    assert sim.snapshot()["nodes"][FAB]["temperature_c"][1] > 60   # залипший датчик
    sim.control(FAB, "set_lock", {"lock": 0, "locked": False}, origin=A)
    sim.record_chat({"to": "operator", "text": "Бокс открыт, стол холодный — забирайте деталь"})
    sim.step()
    sim.control(FAB, "set_door", {"door": 0, "open": True}, origin=OP)
    sim.run_for(12)
    report = next(r for r in sim.crisis_reports() if r["code"] == "№51")
    assert report["outcome"] == "RESOLVED" and not report["penalties"], report


def test_n51_without_message_is_penalized():
    sim = make(jobs=False)
    cooled_part(sim)
    report = play(sim, "51", {}, [at(8, FAB, "set_lock", {"lock": 1, "locked": False})], 40)
    assert report.outcome == "RESOLVED" and report.penalties


def test_real_machines_hold_commands():
    sim = make()
    for node in (PRINTER, CNC):
        sim.update(node, control_override_source="real")
    sim.drain_real_commands()
    sim.trigger_crisis("6")
    queued = []
    for _ in range(30):
        sim.step()
        queued += sim.drain_real_commands()
        if ("printer_3d_01", "pause", None) in queued:
            sim.update(PRINTER, state="PAUSED")                      # так ответил Klipper (адаптер)
            sim.update(CNC, state="PAUSED")
            break
    assert ("cnc_01", "hold", None) in queued
    sim.run_for(20)
    assert sim.get(PRINTER, "hold_reason") == "IL_CO2"
    assert sim.prepare_real_command(PRINTER, "resume") == []         # причина активна — на железо не идёт
    assert sim.get(PRINTER, "resume_attempts") == 1
    sim.control(AUTO, "set_primary_sensor", "climate_sensor_02", origin=A)
    sim.control(AUTO, "reset_interlock", "IL_CO2", origin=A)
    sim.step()
    assert sim.prepare_real_command(PRINTER, "resume") == [("resume", None)]

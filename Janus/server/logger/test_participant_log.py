# logger/test_participant_log.py — python -m pytest logger/test_participant_log.py
import json
from datetime import datetime, timedelta

from participant_log import ParticipantLogger


def _read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _nodes(state="ON", fan="OFF", temp=40.0, events=(), lines_state="ON"):
    return {
        "solar_inverter_01": {"mode": "HYBRID", "fan_state": fan, "inverter_temp_c": temp, "error_code": None,
                              "updated_at": 1.0},
        "smart_panel_01": {"lines": [{"line": 1, "state": lines_state, "current_a": 1.2}]},
        "dome_automation_01": {"events": list(events)},
        "meeting_01": {"recent_messages": [{"text": "не пишется"}]},
    }


def test_changes_snapshots_and_journal(tmp_path):
    log = ParticipantLogger(tmp_path, snapshot_s=60)
    t0 = datetime(2026, 10, 12, 9, 0, 0)
    log.observe(t0, _nodes())
    # штатное переключение вентилятора не пишется, FAULT — пишется
    log.observe(t0 + timedelta(seconds=4), _nodes(fan="ON"))
    log.observe(t0 + timedelta(seconds=8), _nodes(fan="FAULT", lines_state="TRIPPED"))
    # пропавшее число — событие, только если держится NUMBER_STABLE_N опросов
    log.observe(t0 + timedelta(seconds=12), _nodes(fan="FAULT", lines_state="TRIPPED", temp=None))
    log.observe(t0 + timedelta(seconds=16), _nodes(fan="FAULT", lines_state="TRIPPED"))
    for i in range(5):
        log.observe(t0 + timedelta(seconds=20 + 4 * i), _nodes(fan="FAULT", lines_state="TRIPPED", temp=None))
    ev = {"event_id": "AUT-0001", "type": "TRIP", "message": "x", "acknowledged": False}
    log.observe(t0 + timedelta(seconds=61), _nodes(fan="FAULT", lines_state="TRIPPED", temp=None, events=[ev]))
    log.observe(t0 + timedelta(seconds=65), _nodes(fan="FAULT", lines_state="TRIPPED", temp=None,
                                                    events=[{**ev, "acknowledged": True}]))
    log.action(t0 + timedelta(seconds=66), "agent", "smart_panel_01", "reset_protection", 1, True, team=1)
    log.close()

    events = _read(tmp_path / "2026-10-12" / "events.jsonl")
    changes = [(e["node_id"], e["param"], e["from"], e["to"]) for e in events if e["kind"] == "change"]
    assert ("solar_inverter_01", "fan_state", "ON", "FAULT") in changes
    assert ("solar_inverter_01", "fan_state", "OFF", "ON") not in changes
    assert ("smart_panel_01", "lines[0].state", "ON", "TRIPPED") in changes
    gone = [e for e in events if e["kind"] == "change" and e["param"] == "inverter_temp_c"]
    assert len(gone) == 1 and gone[0]["note"] == "показание пропало"
    plc = [e for e in events if e["kind"] == "automation"]
    assert len(plc) == 2 and plc[1]["updated"] is True
    assert not any(e.get("node_id") == "meeting_01" for e in events)
    assert events[-1]["kind"] == "action" and events[-1]["team"] == 1

    snaps = _read(tmp_path / "2026-10-12" / "telemetry.jsonl")
    assert [s["ts"] for s in snaps] == ["2026-10-12T09:00:00", "2026-10-12T09:01:01"]


def test_day_rotation(tmp_path):
    log = ParticipantLogger(tmp_path, snapshot_s=3600)
    log.observe(datetime(2026, 10, 12, 23, 59, 58), _nodes())
    log.observe(datetime(2026, 10, 13, 0, 0, 2), _nodes())   # новый день — снимок в новый файл
    log.close()
    assert (tmp_path / "2026-10-13" / "telemetry.jsonl").exists()

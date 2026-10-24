# week_logs/plan.py
"""
План недели: распорядок жильцов, задания станкам, кризисы и варианты реакции на них.

Неделя покрывает кризисы 90-минутной смены (shift.py) и часть резервных — с прецедентами
из dome_crises.md («След в логе»): правильная реакция, поздняя, ошибочная; где-то жильцы
неверно поняли причину (это видно только в бортовом журнале, данные остаются честными).

День 0 — понедельник. Время — часы купола (сутки 24 ч).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import crew
from . import incidents as inc
from .world import DAY_S, Script, Wait, World

P, C = crew.PRINTER, crew.CNC

# Жильцы купола (вымышленные). gender — для форм глаголов в бортовом журнале.
RESIDENTS: dict[str, dict[str, str]] = {
    "Марина Белова": {"role": "оператор мастерской", "gender": "f"},
    "Андрей Котов": {"role": "инженер-энергетик", "gender": "m"},
    "Илья Сафин": {"role": "техник связи и автоматики", "gender": "m"},
    "Ольга Ренц": {"role": "командир купола", "gender": "f"},
}

# (начало, конец) присутствия Оператора в мастерской
PRESENCE = {0: ("09:00", "18:30"), 1: ("09:00", "18:30"), 2: ("09:00", "18:30"), 3: ("09:00", "18:30"),
            4: ("09:00", "18:30"), 5: ("09:30", "17:00"), 6: ("10:00", "16:30")}
OPERATOR = {d: ("Ольга Ренц" if d >= 5 else "Марина Белова") for d in range(7)}

# Задания: (время запроса, станок, файл)
JOBS: dict[int, list[tuple[str, str, str]]] = {
    0: [("09:20", P, "bracket_v2.gcode"), ("09:40", C, "pcb_power_board.nc"), ("13:20", C, "pcb_sensor_hub.nc"),
        ("13:50", P, "fan_duct.gcode"), ("15:40", C, "front_panel_engrave.nc")],
    1: [("09:30", P, "gear_m1_z24.gcode"), ("10:20", C, "pcb_relay_driver.nc"), ("11:00", P, "cable_clip_x10.gcode"),
        ("14:00", P, "bracket_v2.gcode"), ("16:00", C, "pcb_relay_driver.nc")],
    2: [("09:30", P, "sensor_case.gcode"), ("09:45", C, "pcb_power_board.nc"), ("12:30", C, "pcb_sensor_hub.nc"),
        ("14:30", P, "gear_m1_z24.gcode"), ("15:30", C, "front_panel_engrave.nc")],
    3: [("09:10", P, "gear_m1_z24.gcode"), ("09:20", C, "pcb_power_board.nc"), ("13:30", P, "valve_handle.gcode"),
        ("13:40", C, "pcb_relay_driver.nc")],
    4: [("09:20", P, "bracket_v2.gcode"), ("11:00", C, "front_panel_engrave.nc"), ("11:40", P, "fan_duct.gcode"),
        ("12:00", C, "pcb_sensor_hub.nc"), ("15:00", C, "pcb_relay_driver.nc")],
    5: [("09:40", P, "cable_clip_x10.gcode"), ("10:00", C, "pcb_sensor_hub.nc")],
    6: [("10:30", P, "gear_m1_z24.gcode"), ("12:00", C, "front_panel_engrave.nc")],
}

# Пайка: (начало, минут, режим)
SOLDER: dict[int, list[tuple[str, float, str]]] = {
    0: [("10:30", 45, "SOLDERING"), ("15:00", 40, "SOLDERING"), ("16:10", 10, "HOT_AIR")],
    1: [("10:00", 40, "SOLDERING")],
    2: [("10:30", 60, "SOLDERING"), ("15:00", 30, "SOLDERING")],
    3: [("11:00", 60, "SOLDERING"), ("15:00", 45, "SOLDERING")],
    4: [("09:55", 35, "SOLDERING"), ("14:30", 45, "SOLDERING")],
    5: [("10:30", 30, "SOLDERING")],
    6: [("11:30", 30, "SOLDERING")],
}

BALANCING_DAYS = (1, 4)       # штатная балансировка в конце заряда

# Кризисы: (день, время, сценарий, вариант реакции, понимание причины, параметры)
INCIDENTS: list[tuple[int, str, Any, str, str, dict[str, Any]]] = [
    # Пн
    (0, "10:30", inc.bus_collision, "late", "wrong", {}),
    (0, "13:40", inc.cnc_feed_hold, "ok", "right", {}),
    # Вт
    (1, "02:00", inc.false_outdoor_temp, "ok", "right", {}),
    (1, "06:40", inc.false_condensate, "none", "right", {}),
    (1, "10:25", inc.leakage_cnc, "bad", "right", {}),
    (1, "14:20", inc.fume_trip, "bad", "wrong", {}),
    (1, "20:00", inc.real_co2, "ok", "right", {}),
    (1, "23:30", inc.phantom_load, "late", "right", {}),
    # Ср
    (2, "04:30", inc.isolation_fault, "ok", "right", {}),
    (2, "10:50", inc.false_print_complete, "bad", "right", {}),
    (2, "12:50", inc.phantom_co2, "ok", "right", {}),
    (2, "16:30", inc.lighting_flicker, "late", "wrong", {}),
    (2, "21:00", inc.time_desync, "late", "right", {}),
    # Чт
    (3, "07:50", inc.stuck_vane, "ok", "right", {"fixed_day": 4, "fixed_hhmm": "09:10"}),
    (3, "09:35", inc.false_condensate, "ok", "right", {}),
    (3, "11:30", inc.false_trip_workshop, "ok", "right", {"drop": 0}),
    (3, "14:05", inc.fablab_voc, "ok", "right", {}),
    (3, "15:30", inc.lock_trap, "ok", "right", {}),
    (3, "19:10", inc.false_low_soc, "late", "wrong", {}),
    # Пт
    (4, "02:00", inc.edge_memory_leak, "ok", "right", {}),
    (4, "10:05", inc.false_trip_workshop, "ok", "right", {"drop": 1}),
    (4, "12:10", inc.false_fire, "ok", "right", {}),
    (4, "13:40", inc.false_co2_drop, "late", "wrong", {}),
    (4, "19:30", inc.inverter_fan, "ok", "right", {}),
    # Сб
    (5, "03:00", inc.telemetry_loss, "late", "right", {}),
    (5, "09:50", inc.false_nozzle, "ok", "right", {}),
    (5, "10:12", inc.leakage_cnc, "ok", "right", {}),
    (5, "11:20", inc.sfp_degradation, "ok", "right", {}),
    (5, "13:00", inc.chain_overload, "bad", "wrong", {}),
    (5, "21:00", inc.night_without_reserve, "late", "right", {}),
    # Вс
    (6, "10:05", inc.sfp_replace, "ok", "right", {}),
    (6, "08:00", inc.false_balancing, "ok", "right", {}),
    (6, "12:40", inc.false_rcd_lighting, "ok", "right", {}),
    (6, "13:30", inc.weather_loss, "ok", "right", {"hours": 2.8}),
    (6, "15:00", inc.storage_fire, "ok", "right", {}),
]

# Бортовой журнал: (день, время, автор, итоги недели). Запись пишется в конце рабочего дня.
LOGBOOK: list[tuple[int, str, str, bool]] = [
    (0, "18:50", "Марина Белова", False), (1, "18:40", "Андрей Котов", False), (2, "18:45", "Илья Сафин", False),
    (3, "18:50", "Марина Белова", False), (4, "18:40", "Андрей Котов", False), (5, "17:10", "Ольга Ренц", False),
    (6, "16:40", "Ольга Ренц", False), (6, "21:30", "Илья Сафин", True),
]


@dataclass
class WeekPlan:
    days: int = 7
    sfp_replaced: bool = False
    incidents: list = field(default_factory=lambda: list(INCIDENTS))

    def operator_of(self, day: int) -> str:
        return OPERATOR.get(day % 7, "Марина Белова")

    def presence(self, day: int) -> tuple[str, str]:
        return PRESENCE.get(day % 7, ("09:00", "18:30"))

    def operator_present(self, w: World) -> bool:
        start, end = self.presence(w.day)
        return w.at(w.day, start) <= w.t < w.at(w.day, end)

    def night_notes(self, w: World) -> bool:
        return any(f.key in ("n01", "k01", "n21", "k08", "n36") for f in w.facts.get(w.day, []))


def _at(w: World, day: int, hhmm: str, gen_factory) -> Script:
    yield Wait(max(0.0, w.at(day, hhmm) - w.t))
    yield from gen_factory()


def schedule(w: World, plan: WeekPlan) -> None:
    """Ставит в планировщик распорядок всех дней, предвестники и кризисы."""
    w.spawn("part_pickup", crew.part_pickup(w))
    w.spawn("cnc_done", crew.cnc_done(w))
    w.spawn("printer_done", crew.printer_done(w))
    w.spawn("maintenance", crew.maintenance(w))
    w.spawn("energy_manager", crew.energy_manager(w))
    w.spawn("drift_fuel", inc.drift_fuel_water(w, 120.0, 640.0, 5, "18:00"))
    w.spawn("drift_sfp", inc.drift_sfp(w, 5, "11:00"))
    w.spawn("drift_fan", _at(w, 2, "09:00", lambda: inc.drift_fan_wear(w, 4, "19:20")))

    for day in range(plan.days):
        start, end = plan.presence(day)
        w.spawn(f"arrive{day}", _at(w, day, start, lambda: crew.arrive(w)))
        w.spawn(f"leave{day}", _at(w, day, end, lambda: crew.leave(w)))
        w.spawn(f"day_mode{day}", _at(w, day, "07:05", lambda: crew.day_mode(w)))
        w.spawn(f"night_mode{day}", _at(w, day, "22:30", lambda: crew.night_mode(w)))
        if day in BALANCING_DAYS:
            w.spawn(f"balancing{day}", _at(w, day, "12:30", lambda: crew.balancing(w)))
        for hhmm, machine, file_name in JOBS.get(day, []):
            w.spawn(f"job{day}_{file_name}",
                    _at(w, day, hhmm, lambda m=machine, f=file_name: crew.job(w, m, f)))
        for hhmm, minutes, mode in SOLDER.get(day, []):
            w.spawn(f"solder{day}_{hhmm}", _at(w, day, hhmm, lambda mi=minutes, mo=mode: crew.solder(w, mi, mo)))

    for day, hhmm, fn, variant, belief, kwargs in plan.incidents:
        if day >= plan.days:
            continue
        w.spawn(f"{fn.__name__}{day}",
                _at(w, day, hhmm, lambda f=fn, v=variant, b=belief, k=kwargs: f(w, v, b, **k)))


def logbook_times(plan: WeekPlan) -> list[tuple[float, str, bool]]:
    return [(day * DAY_S + int(t[:2]) * 3600 + int(t[3:]) * 60, author, summary)
            for day, t, author, summary in LOGBOOK if day < plan.days]

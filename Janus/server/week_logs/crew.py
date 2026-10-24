# week_logs/crew.py
"""
Обычная жизнь купола за неделю: задания Оператора на печать и фрезеровку, пайка на
рабочем месте, снятие деталей из бокса ФабЛаб, энергорежим день/ночь, штатная
балансировка АКБ и обслуживание мелких отказов. Расписание дня — в plan.py.

Агент (команды с ключом команды) и Оператор (физические действия) действуют так же,
как в смене: агент запускает задания по сообщениям Оператора в песочнице, Оператор
работает руками. Узлы, занятые сценарием кризиса (World.own), распорядок не трогает.
"""
from __future__ import annotations

from typing import Any

from .world import DAY_S, Script, Until, Wait, World

PRINTER, CNC, PANEL, INVERTER = "printer_3d_01", "cnc_01", "smart_panel_01", "solar_inverter_01"
FABLAB, AUTO = "thermal_insulation_01", "dome_automation_01"

JOB_TITLES = {   # что это за деталь (именительный падеж)
    "bracket_v2.gcode": "кронштейны для стойки датчиков",
    "fan_duct.gcode": "воздуховод вентилятора",
    "valve_handle.gcode": "рукоятки клапанов",
    "sensor_case.gcode": "корпус датчика",
    "gear_m1_z24.gcode": "шестерня z24",
    "cable_clip_x10.gcode": "клипсы для кабелей",
    "pcb_power_board.nc": "плата питания",
    "pcb_sensor_hub.nc": "плата хаба датчиков",
    "pcb_relay_driver.nc": "плата драйвера реле",
    "front_panel_engrave.nc": "гравировка передней панели",
}
JOB_HOURS = {"bracket_v2.gcode": 2.0, "fan_duct.gcode": 1.5, "valve_handle.gcode": 3.0, "sensor_case.gcode": 4.0,
             "gear_m1_z24.gcode": 1.0, "cable_clip_x10.gcode": 0.75, "pcb_power_board.nc": 1.2,
             "pcb_sensor_hub.nc": 0.8, "pcb_relay_driver.nc": 1.5, "front_panel_engrave.nc": 0.5}


def pick(w: World, *options: str) -> str:
    return w.rng.choice(options)


def operator_name(w: World) -> str:
    return w.plan.operator_of(w.day)


def op_say(w: World, text: str) -> None:
    w.say(text, who="operator", name=operator_name(w), to="agent")


def agent_say(w: World, text: str) -> None:
    w.say(text, who="agent", to="operator")


def machine_free(w: World, machine: str) -> bool:
    if machine in w.owned or not w.tv(machine, "online"):
        return False
    state = w.tv(machine, "state")
    if machine == PRINTER:
        return state in ("IDLE", "COMPLETED") and w.tv(PRINTER, "bed_clear") is not False
    return state == "IDLE"


# ---------------------------------------------------------------------------
# Задания
# ---------------------------------------------------------------------------

def job(w: World, machine: str, file_name: str) -> Script:
    """Оператор просит запустить задание — агент проверяет станок и запускает."""
    what = JOB_TITLES.get(file_name, file_name)
    device = "принтер" if machine == PRINTER else "фрезер"
    op_say(w, pick(w, f"Запусти, пожалуйста, {file_name} ({what})." if machine == PRINTER
                   else f"Запусти {file_name} ({what}), заготовку закрепила.",
                   f"Следующее задание: {file_name} — {what}.",
                   f"{device.capitalize()} свободен? Нужен {file_name} ({what})."))
    yield Wait(w.rng.uniform(20, 90))
    ok = yield Until(lambda: machine_free(w, machine), timeout_s=3 * 3600, every_s=30)
    if not ok:
        agent_say(w, f"{device.capitalize()} так и не освободился — {file_name} не запущен, перенесём на завтра.")
        return
    if not w.tv(machine, "homed"):
        w.agent(machine, "home")
        yield Wait(8)
    if w.agent(machine, "start_job", file_name):
        hours = JOB_HOURS.get(file_name, 1.0)
        agent_say(w, pick(w, f"Запустил {file_name} на {'принтере' if machine == PRINTER else 'фрезере'}, "
                             f"ориентировочно {hours:g} ч.",
                          f"{file_name}: задание запущено.",
                          f"Принял. {device.capitalize()} работает по {file_name}, слежу."))
        w.fact("job_started", machine=machine, file=file_name)
    else:
        agent_say(w, f"{file_name} не запустился — см. ошибку станка, разбираюсь.")


def _unlock(w: World) -> Script:
    """Открыть замок 0 бокса и проверить по locks (команда может потеряться — №22)."""
    for _ in range(2):
        w.agent(FABLAB, "set_lock", {"lock": 0, "locked": False})
        yield Wait(8)
        if w.tv(FABLAB, "locks[0]") == "UNLOCKED":
            return


def part_pickup(w: World) -> Script:
    """Готовая деталь в боксе ФабЛаб: стол остыл — агент открывает замок, Оператор забирает."""
    while True:
        yield Until(lambda: (w.tv(PRINTER, "state") == "COMPLETED" and w.tv(PRINTER, "bed_clear") is False
                             and w.plan.operator_present(w) and FABLAB not in w.owned and PRINTER not in w.owned),
                    timeout_s=DAY_S * 8, every_s=60)
        file_name = w.tv(PRINTER, "file_name")
        cooled = yield Until(lambda: float(w.tv(PRINTER, "bed_temp_c") or 99.0) < 38.0, timeout_s=3600, every_s=30)
        if not cooled or FABLAB in w.owned:
            continue
        op_say(w, pick(w, f"Вижу, {file_name} допечатался. Откроешь бокс?",
                       "Печать закончилась, заберу деталь — разблокируй бокс.",
                       "Готово на принтере? Иду за деталью."))
        yield Wait(w.rng.uniform(20, 60))
        reported = False
        t0 = w.t
        while w.tv(FABLAB, "locks[0]") != "UNLOCKED" and w.t - t0 < 3 * 3600 and FABLAB not in w.owned:
            yield from _unlock(w)
            if w.tv(FABLAB, "locks[0]") == "UNLOCKED":
                break
            if not reported:
                reported = True
                agent_say(w, "Замок 0 бокса не открывается: set_lock отвечает success, а locks[0] остаётся "
                             "LOCKED. Разбираюсь, пробую ещё.")
                w.fact("lock_lost", crc=w.tv(FABLAB, "crc_errors"), poll=w.tv(FABLAB, "poll_interval_ms"))
            yield Wait(900)
        if w.tv(FABLAB, "locks[0]") != "UNLOCKED":
            continue
        agent_say(w, pick(w, f"Стол {w.tv(PRINTER, 'bed_temp_c')} °C, замок 0 открыт — можно забирать.",
                          "Бокс разблокирован (замок 0), стол остыл." if not reported else
                          "Замок 0 наконец открылся, можно забирать."))
        yield Wait(w.rng.uniform(40, 120))
        if w.operator(FABLAB, "set_door", {"door": 0, "open": True}):
            yield Wait(w.rng.uniform(60, 150))
            w.operator(FABLAB, "set_door", {"door": 0, "open": False})
            op_say(w, pick(w, "Деталь забрала, дверь закрыла.", "Сняла, стол свободен.", "Забрала, всё ок."))
            yield Wait(10)
            w.agent(FABLAB, "set_lock", {"lock": 0, "locked": True})
        yield Wait(300)


def cnc_done(w: World) -> Script:
    """Фрезер закончил плату — Оператор снимает её и пишет об этом."""
    while True:
        yield Until(lambda: w.tv(CNC, "state") == "IDLE" and float(w.tv(CNC, "progress_pct") or 0) >= 100.0
                    and w.tv(CNC, "file_name") is not None, timeout_s=DAY_S * 8, every_s=60)
        name = w.tv(CNC, "file_name")
        yield Until(lambda: w.plan.operator_present(w), timeout_s=DAY_S, every_s=300)
        yield Wait(w.rng.uniform(60, 600))
        op_say(w, pick(w, f"{name}: плату сняла, качество нормальное.", f"Фрезер закончил {name}, забрала.",
                       f"Сняла {name}, стол фрезера свободен."))
        w.fact("job_done", machine=CNC, file=name)
        yield Until(lambda: w.tv(CNC, "file_name") != name or float(w.tv(CNC, "progress_pct") or 0) < 100.0,
                    timeout_s=DAY_S * 8, every_s=120)


def printer_done(w: World) -> Script:
    while True:
        yield Until(lambda: w.tv(PRINTER, "state") == "COMPLETED", timeout_s=DAY_S * 8, every_s=60)
        w.fact("job_done", machine=PRINTER, file=w.tv(PRINTER, "file_name"))
        yield Until(lambda: w.tv(PRINTER, "state") != "COMPLETED", timeout_s=DAY_S * 8, every_s=120)


# ---------------------------------------------------------------------------
# Рабочее место и присутствие
# ---------------------------------------------------------------------------

def solder(w: World, minutes: float, mode: str = "SOLDERING") -> Script:
    if PANEL in w.owned:
        return
    w.operator(PANEL, "set_workstation", mode)
    yield Wait(minutes * 60.0)
    if w.truth(PANEL, "control_workstation") == mode:
        w.operator(PANEL, "set_workstation", "IDLE")


def arrive(w: World) -> Script:
    w.operator(PANEL, "set_workstation", "IDLE")
    op_say(w, pick(w, "Доброе утро, я в мастерской.", "На месте, начинаю смену.", "Привет, я в куполе. Что по системам?"))
    yield Wait(w.rng.uniform(30, 120))
    soc = round(float(w.tv(INVERTER, "battery_soc_pct") or 0))
    agent_say(w, pick(w, f"Доброе утро. АКБ {soc} %, инвертор {w.tv(INVERTER, 'mode')}, станки свободны.",
                      f"Привет. Ночь прошла {'спокойно' if not w.plan.night_notes(w) else 'с событиями, см. журнал'}; "
                      f"SOC {soc} %.",
                      f"Доброе. Сводка: SOC {soc} %, щиток — включено линий {w.tv(PANEL, 'lines_on_count')}."))


def leave(w: World) -> Script:
    if PANEL not in w.owned:
        w.operator(PANEL, "set_workstation", "OFF")
    op_say(w, pick(w, "Ухожу, рабочее место выключила.", "Конец смены, до завтра.", "Всё, я закончила на сегодня."))
    yield Wait(1)


# ---------------------------------------------------------------------------
# Энергия
# ---------------------------------------------------------------------------

def soc(w: World) -> float:
    return float(w.tv(INVERTER, "battery_soc_pct") or 0.0)


def night_mode(w: World) -> Script:
    """На ночь — нагрузка от сети (GRID_ONLY), чтобы не разряжать АКБ до сброса нагрузки."""
    if INVERTER in w.owned:
        return
    if w.tv(INVERTER, "grid_state") != "AVAILABLE" or w.tv(INVERTER, "mode") != "HYBRID":
        return
    if w.agent(INVERTER, "set_mode_grid_only"):
        agent_say(w, f"Перевёл инвертор на ночь в GRID_ONLY, SOC {soc(w):.0f} %.")
    yield Wait(1)


def day_mode(w: World) -> Script:
    """Утром — HYBRID, если АКБ есть чем разряжать; иначе подождать солнца (energy_manager)."""
    if INVERTER in w.owned or w.tv(INVERTER, "mode") == "HYBRID" or soc(w) < 50.0:
        return
    w.agent(INVERTER, "set_mode_hybrid")
    yield Wait(1)


def energy_manager(w: World) -> Script:
    """В HYBRID сеть только покрывает дефицит, АКБ от неё не заряжается. При SOC < 35 % нагрузка
    переводится на сеть (GRID_ONLY: PV заряжает АКБ), при SOC > 75 % и солнце — обратно в HYBRID."""
    while True:
        yield Wait(300)
        if INVERTER in w.owned or w.tv(INVERTER, "error_code"):
            continue
        night = w.hour >= 22.5 or w.hour < 7.0
        mode, grid = w.tv(INVERTER, "mode"), w.tv(INVERTER, "grid_state")
        if mode == "HYBRID" and soc(w) < 35.0 and grid == "AVAILABLE":
            if w.agent(INVERTER, "set_mode_grid_only"):
                agent_say(w, f"SOC {soc(w):.0f} % — перевёл нагрузку на сеть (GRID_ONLY), АКБ заряжается от панелей.")
                w.fact("grid_only", soc=round(soc(w)))
        elif mode == "GRID_ONLY" and not night and soc(w) > 75.0 and float(w.tv(INVERTER, "pv_power_w") or 0) > 300:
            if w.agent(INVERTER, "set_mode_hybrid"):
                agent_say(w, f"АКБ {soc(w):.0f} %, солнце есть — вернул инвертор в HYBRID.")


def balancing(w: World) -> Script:
    """Штатная балансировка — только в конце заряда (SOC > 95 %)."""
    ok = yield Until(lambda: float(w.tv(INVERTER, "battery_soc_pct") or 0) > 96.0
                     and w.tv(INVERTER, "balancing_state") != "BALANCING" and INVERTER not in w.owned,
                     timeout_s=4 * 3600, every_s=120)
    if ok and w.agent(INVERTER, "start_cell_balancing", 30):
        w.fact("balancing", soc=w.tv(INVERTER, "battery_soc_pct"))


# ---------------------------------------------------------------------------
# Обслуживание мелких отказов (агент проверяет телеметрию раз в минуту)
# ---------------------------------------------------------------------------

def hold_cleared(w: World, machine: str) -> bool:
    """По телеметрии: ушла ли причина удержания станка (то, что видит агент)."""
    reason = w.tv(machine, "hold_reason") or ""
    if reason.startswith("IL_") and reason != "IL_VOC_BOX":
        return w.tv(AUTO, f"interlocks.{reason}.state") != "TRIPPED"
    if reason == "IL_VOC_BOX":
        return float((w.tv(FABLAB, "voc_index") or [500])[0] or 500) < 70
    if reason == "UNDERVOLTAGE":
        return float(w.tv(PANEL, "bus_voltage_v") or 0) > 215
    return False


def maintenance(w: World) -> Script:
    seen: dict[str, float] = {}
    retry_at: dict[str, float] = {}
    oc_trips = int((w.tv(AUTO, "interlocks.IL_OVERCURRENT") or {}).get("trips") or 0)

    def first_seen(key: str, active: bool) -> float:
        """Сколько секунд держится проблема; после реакции следующая — не раньше чем через 20 мин."""
        if not active:
            seen.pop(key, None)
            return 0.0
        seen.setdefault(key, w.t)
        if w.t < retry_at.get(key, 0.0):
            return 0.0
        return w.t - seen[key]

    def acted(key: str) -> None:
        retry_at[key] = w.t + 1200.0

    while True:
        yield Wait(60)
        free = lambda n: n not in w.owned  # noqa: E731
        # ветрогенератор после YAW_ERROR
        if free("wind_turbine_01") and first_seen("wind", w.tv("wind_turbine_01", "fault_code") == "YAW_ERROR") > 900:
            w.agent("wind_turbine_01", "turn_on")
            w.fact("wind_reset")
            acted("wind")
        # сервисы edge
        services = w.tv("edge_compute_01", "services") or {}
        for service, state in services.items():
            if free("edge_compute_01") and first_seen(f"edge_{service}", state == "FAILED") > 120:
                w.agent("edge_compute_01", "restart_service", service)
                w.fact("edge_restart", service=service)
                acted(f"edge_{service}")
        # дизель не завёлся с первого раза (не вода в топливе)
        crank = w.tv("dizel_1", "fault_code") == "START_FAILURE" and w.tv("dizel_1", "fault_reason") == "CRANK"
        if free("dizel_1") and first_seen("diesel", crank) > 30:
            w.agent("dizel_1", "turn_off")
            yield Wait(30)
            w.agent("dizel_1", "turn_on")
            w.fact("diesel_crank")
            acted("diesel")
        # вентиляция, охлаждение, насос
        vent = w.tv("supply_ventilation_01") or {}
        if free("supply_ventilation_01"):
            if float(vent.get("filter_clog_pct") or 0) > 85.0 and w.plan.operator_present(w):
                w.operator("supply_ventilation_01", "replace_filter")
                w.fact("vent_filter")
            elif first_seen("vent", bool(vent.get("alarm"))) > 300:
                w.agent("supply_ventilation_01", "reset_alarm")
                w.agent("supply_ventilation_01", "turn_on")
                w.fact("vent_alarm", code=vent.get("alarm_code"))
                acted("vent")
        for node_id in ("equipment_cooling_01", "water_pump_01"):
            if free(node_id) and first_seen(node_id, bool(w.tv(node_id, "alarm"))) > 300:
                if w.agent(node_id, "reset_alarm"):
                    w.agent(node_id, "turn_on")
                acted(node_id)
        # водоочистка
        if free("water_filter_01") and first_seen("wf", bool(w.tv("water_filter_01", "warning_active"))) > 600:
            if float(w.tv("water_filter_01", "hours_to_replacement") or 999) < 24:
                w.agent("water_filter_01", "switch_to_reserve")
            w.agent("water_filter_01", "reset_warning")
            acted("wf")
        # тревоги доступа и внешних датчиков, вызванные сбоем датчика
        if free("access_control_01") and first_seen("access", bool(w.tv("access_control_01", "alarm"))) > 600:
            w.fact("access_alarm", reason=w.tv("access_control_01", "alarm_reason"))
            w.agent("access_control_01", "reset_alarm")
            acted("access")
        for node_id in ("radiation_sensor_01", "chem_sensor_01"):
            alarm = w.tv(node_id, "alarm_state") not in (None, "NORMAL") and w.tv(node_id, "sensor_state") == "OK"
            if free(node_id) and first_seen(node_id, alarm) > 900:
                w.agent(node_id, "reset_alarm")
                acted(node_id)
        # клапаны оболочки
        if free("dome_sealing_01") and first_seen("seal", bool(w.tv("dome_sealing_01", "fault_code"))) > 600:
            w.agent("dome_sealing_01", "reset_error")
            acted("seal")
        # ALARM фрезера (концевик) вне кризисов: HOME, Оператор перезапустит плату
        cnc_alarm = w.tv(CNC, "state") == "ALARM" and w.tv(CNC, "online") and w.tv(CNC, "hold_level") != "ALARM"
        if free(CNC) and first_seen("cnc_alarm", bool(cnc_alarm)) > 120:
            name, code = w.tv(CNC, "file_name"), w.tv(CNC, "alarm_code")
            if w.agent(CNC, "home"):
                agent_say(w, f"Фрезер ушёл в {code or 'ALARM'} на {name}; выполнил HOME.")
                w.fact("cnc_alarm", file=name, code=code)
            acted("cnc_alarm")
        # IL_OVERCURRENT отключил линию мастерской (фен + вытяжка больше уставки) — объяснить и вернуть.
        # Поштучный интерлок линии перевзводится сразу после отключения — видно по счётчику trips.
        il_oc = w.tv(AUTO, "interlocks.IL_OVERCURRENT") or {}
        trips = int(il_oc.get("trips") or 0)
        if trips > oc_trips:
            oc_trips = trips
            if free(PANEL) and w.line(1) == "OFF":
                limit = (w.tv(AUTO, "line_current_limits_a") or {}).get("1", 2.5)
                agent_say(w, f"Линию 1 (мастерская) отключил IL_OVERCURRENT: ток больше 90 % уставки {limit} А "
                             "дольше 2 минут. Фен с вытяжкой столько не тянут — выключите, пожалуйста, фен, "
                             "я верну линию.")
                if w.plan.operator_present(w):
                    yield Wait(60)
                    if w.truth(PANEL, "control_workstation") == "HOT_AIR":
                        w.operator(PANEL, "set_workstation", "IDLE")
                    op_say(w, "Фен выключила.")
                w.agent(PANEL, "line_on", 1)
                w.fact("overcurrent", limit=limit)
        # производственная линия выключена без причины (никто не вернул после сброса) — включить
        for n in (1, 3, 4):
            off = w.line(n) == "OFF" and w.plan.operator_present(w) and not any(
                (st or {}).get("state") == "TRIPPED" for st in (w.tv(AUTO, "interlocks") or {}).values())
            if free(PANEL) and first_seen(f"line{n}_off", off) > 900:
                if w.agent(PANEL, "line_on", n):
                    agent_say(w, f"Линия {n} была выключена без активной защиты и интерлока — включил.")
                acted(f"line{n}_off")
        # станок на удержании сервера: продолжить, только когда видимая причина ушла
        for machine in (PRINTER, CNC):
            held = w.tv(machine, "state") == "PAUSED" and w.tv(machine, "hold_level") == "WARNING"
            if free(machine) and first_seen(f"held_{machine}", held) > 600 and hold_cleared(w, machine):
                if w.agent(machine, "resume"):
                    agent_say(w, f"{machine}: причина удержания {w.tv(machine, 'hold_reason')} ушла — продолжил.")
                acted(f"held_{machine}")
        # замки доступа: батарейки меняет Оператор
        doors: dict[str, Any] = w.tv("access_control_01", "doors") or {}
        for door, st in doors.items():
            low = float(st.get("lock_battery_pct") or 100) < 25 and w.plan.operator_present(w)
            if first_seen(f"lock_{door}", low) > 0:
                w.operator("access_control_01", "replace_lock_battery", door)
                acted(f"lock_{door}")

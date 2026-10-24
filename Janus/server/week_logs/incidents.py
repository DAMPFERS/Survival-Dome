# week_logs/incidents.py
"""
Кризисы недели — прецеденты для RAG (dome_crises.md, «След в логе»).

Сценарий кризиса: запускает кризис из каталога симулятора (в журнал участника не
попадает), ждёт, как ждали бы люди, и реагирует так, как определено вариантом:
    ok    — правильно и вовремя;
    late  — правильно, но поздно (последствие успело наступить или почти наступило);
    bad   — ошибочно (слепой resume, сброс УЗО без осмотра, вера ложному показанию...);
    none  — никто не реагировал (кризис прошёл сам).
belief — как жильцы поняли причину (right | wrong) — это только бортовой журнал.

Сценарий записывает факт дня (World.fact) — из фактов собирается бортовой журнал.
Предвестники (износ вентилятора, вода в топливе, старение SFP) — фоновые сценарии
drift_*: они меняют входы узлов постепенно, как это было бы на настоящем куполе.
"""
from __future__ import annotations

from typing import Any, Callable

from .crew import agent_say, op_say
from .world import Script, Until, Wait, World

PRINTER, CNC, PANEL, INVERTER, BATTERY = ("printer_3d_01", "cnc_01", "smart_panel_01", "solar_inverter_01",
                                          "battery_01")
FABLAB, AUTO, WEATHER, NET, EDGE = ("thermal_insulation_01", "dome_automation_01", "weather_station_01",
                                    "network_link_01", "edge_compute_01")
FUME, VENT, TANK, DIESEL = "fume_extraction_01", "supply_ventilation_01", "fuel_tank_01", "dizel_1"
CLIMATE = ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03")


def trust(node: str, param: str, level: str = "UNRELIABLE") -> dict[str, Any]:
    return {"node": node, "param": param, "trust": level}


def interlock(w: World, il: str, key: str = "state") -> Any:
    return w.tv(AUTO, f"interlocks.{il}.{key}")


def until_time(w: World, day: int, hhmm: str) -> Wait:
    return Wait(max(0.0, w.at(day, hhmm) - w.t))


def num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def restore_lines(w: World) -> list[int]:
    """Включить линии, оставшиеся выключенными после сброса нагрузки (кроме аварийного света)."""
    back = []
    for n in (1, 2, 3, 4, 5, 6, 8):
        if w.line(n) == "OFF" and w.agent(PANEL, "line_on", n):
            back.append(n)
    return back


def resume_held(w: World, machine: str) -> bool:
    if w.tv(machine, "state") == "PAUSED" and w.tv(machine, "hold_level") == "WARNING":
        return w.agent(machine, "resume")
    return False


# ---------------------------------------------------------------------------
# Предвестники (фоновые изменения в течение недели)
# ---------------------------------------------------------------------------

def drift_fuel_water(w: World, start_ppm: float, end_ppm: float, until_day: int, until_hhmm: str) -> Script:
    """Вода в топливе дизеля растёт всю неделю (конденсат в полупустом баке) — К1."""
    t0, t1 = w.t, w.at(until_day, until_hhmm)
    while w.t < t1:
        k = (w.t - t0) / (t1 - t0)
        target = start_ppm + (end_ppm - start_ppm) * k
        if TANK not in w.owned and num(w.truth(TANK, "water_content_ppm")) < target:
            w.sim.ctx.update(TANK, water_content_ppm=round(target, 3))
        yield Wait(1800)


def drift_fan_wear(w: World, until_day: int, until_hhmm: str) -> Script:
    """Подшипник вентилятора инвертора изнашивается: ток вентилятора растёт за 2 дня до отказа (№31)."""
    t0, t1 = w.t, w.at(until_day, until_hhmm)
    while w.t < t1:
        k = (w.t - t0) / (t1 - t0)
        w.sim.ctx.update(INVERTER, control_fan_wear=round(0.15 + 0.85 * k ** 1.5, 3))
        yield Wait(1800)


def drift_sfp(w: World, until_day: int, until_hhmm: str, end: float = 0.27) -> Script:
    """Лазер SFP медиаконвертера стареет: rx_power_dbm медленно падает всю неделю (№23)."""
    t0, t1 = w.t, w.at(until_day, until_hhmm)
    while True:
        k = min(1.0, (w.t - t0) / (t1 - t0))
        if NET not in w.owned and not w.plan.sfp_replaced:
            w.sim.ctx.update(NET, control_sfp_degradation=round(0.02 + (end - 0.02) * k, 4))
        if w.plan.sfp_replaced:
            return
        yield Wait(1800)


# ---------------------------------------------------------------------------
# Энергетика
# ---------------------------------------------------------------------------

def phantom_load(w: World, v: str, b: str) -> Script:
    """№1. Наводка на резервной линии 8 в сырую ночь: +50 Вт без нагрузки."""
    w.trigger("n01_phantom_load")
    start = w.clock()
    yield until_time(w, w.day + 1 if w.hour > 12 else w.day, "08:15")
    watts = w.line(8, "power_w")
    w.agent(PANEL, "line_off", 8)
    agent_say(w, f"Резервная линия 8 с {start} потребляет ~{watts} Вт, хотя нагрузки на ней нет. Отключил. "
                 "Похоже на утечку — посмотрите клеммник линии 8.")
    yield until_time(w, w.day, "09:40")
    w.operator(PANEL, "inspect_line", 8)
    op_say(w, "Клеммник резервной линии окислен и влажный — зачистила, просушила.")
    yield Wait(300)
    w.agent(PANEL, "line_on", 8)
    yield Wait(120)
    w.fact("n01", v, b, since=start, watts=watts, after=w.line(8, "power_w"))


def false_balancing(w: World, v: str, b: str) -> Script:
    """№2. BMS запускает балансировку при среднем SOC — заряд заблокирован."""
    ok = yield Until(lambda: 40.0 <= num(w.tv(INVERTER, "battery_soc_pct")) <= 85.0
                     and num(w.tv(INVERTER, "pv_power_w")) > 200, timeout_s=8 * 3600, every_s=300)
    if not ok:
        return
    w.own(INVERTER)
    w.trigger("n02_false_balancing", {"minutes": 18.0})
    soc = round(num(w.tv(INVERTER, "battery_soc_pct")))
    yield Wait(150 if v == "ok" else 1300)
    if w.tv(INVERTER, "balancing_state") == "BALANCING":
        w.agent(INVERTER, "cancel_balancing")
    lost = w.tv(INVERTER, "mppt_state")
    w.release(INVERTER)
    w.fact("n02", v, b, soc=soc, mppt=lost)


def isolation_fault(w: World, v: str, b: str) -> Script:
    """№11. Туман ночью — E09 (изоляция PV) к рассвету; сброс после того, как влажность ушла."""
    w.trigger("n11_isolation_fault", {"fog_s": 3 * 3600.0, "humidity_pct": 97.0, "duration_s": 6 * 3600.0})
    yield Until(lambda: w.tv(INVERTER, "error_code") == "E09", timeout_s=3600, every_s=30)
    yield until_time(w, w.day, "07:35")
    hum = w.tv(WEATHER, "humidity_pct")
    yield Until(lambda: num(w.tv(WEATHER, "humidity_pct"), 99) < 88, timeout_s=3 * 3600, every_s=120)
    w.agent(INVERTER, "reset_error")
    yield Wait(300)
    w.fact("n11", v, b, humidity=hum, pv=w.tv(INVERTER, "pv_power_w"))


def false_low_soc(w: World, v: str, b: str) -> Script:
    """№32. Датчик напряжения на клеммах АКБ: SOC 70 → 20 % за один отсчёт, IL_LOAD_SHED."""
    w.own(INVERTER)
    soc0, volt0 = round(num(w.tv(INVERTER, "battery_soc_pct"))), w.tv(BATTERY, "voltage_v")
    w.trigger("n32_false_low_soc")
    yield Until(lambda: interlock(w, "IL_LOAD_SHED") == "TRIPPED", timeout_s=900, every_s=8)
    shed = interlock(w, "IL_LOAD_SHED", "shed_lines")
    yield Wait(55 * 60 if v != "ok" else 120)
    if v == "ok":
        w.agent(AUTO, "set_data_trust", trust(BATTERY, "soc_pct"))
        w.agent(AUTO, "set_data_trust", trust(INVERTER, "battery_soc_pct"))
    w.agent(AUTO, "reset_interlock", "IL_LOAD_SHED")
    yield Wait(20)
    restore_lines(w)
    if w.tv(DIESEL, "state") == "ON" and w.tv(INVERTER, "grid_state") == "AVAILABLE":
        w.agent(DIESEL, "turn_off")
    agent_say(w, f"SOC снова {num(w.tv(INVERTER, 'battery_soc_pct')):.0f} %, IL_LOAD_SHED снят, линии {shed} включены.")
    w.release(INVERTER)
    w.fact("n32", v, b, soc_before=soc0, volt=volt0, lines=shed)
    yield Wait(1800)
    if v == "ok":
        w.agent(AUTO, "set_data_trust", trust(BATTERY, "soc_pct", "TRUSTED"))
        w.agent(AUTO, "set_data_trust", trust(INVERTER, "battery_soc_pct", "TRUSTED"))


def inverter_fan(w: World, v: str, b: str) -> Script:
    """№31. Вентилятор инвертора встал (износ подшипника) — снизить нагрузку до замены."""
    w.trigger("n31_inverter_fan_failure")
    t0 = num(w.tv(INVERTER, "inverter_temp_c"))
    yield Wait(480)
    t1 = num(w.tv(INVERTER, "inverter_temp_c"))
    w.agent(PANEL, "line_off", 8)
    w.agent(PANEL, "line_off", 6)
    w.agent(PANEL, "line_on", 7)
    agent_say(w, f"Вентилятор инвертора встал: fan_state {w.tv(INVERTER, 'fan_state')}, ток 0, температура "
                 f"{t0:.0f} → {t1:.0f} °C за 8 мин. Снизил нагрузку (свет на аварийном, резерв выключен). "
                 "Нужна замена вентилятора (solar_inverter_01.replace_fan).")
    yield Wait(1800)
    peak = num(w.tv(INVERTER, "inverter_temp_c"))
    w.fact("n31", v, b, t0=round(t0), t1=round(t1), peak=round(peak))
    yield until_time(w, w.day + 1, "08:40")
    w.operator(INVERTER, "replace_fan")
    op_say(w, "Поменяла вентилятор инвертора, старый гремел подшипником.")
    yield Wait(120)
    w.agent(PANEL, "line_on", 6)
    w.agent(PANEL, "line_off", 7)
    w.agent(PANEL, "line_on", 8)
    w.fact("fan_replaced", "ok", b)


def chain_overload(w: World, v: str, b: str) -> Script:
    """К3. Нагрев стола принтера + пуск шпинделя + включение линии мастерской с феном
    (пусковой ток) одновременно → E07, блэкаут; при возврате всё стартует разом — второй E07."""
    w.own(PRINTER, CNC, PANEL)
    op_say(w, "Ухожу на обед — выключи, пожалуйста, линию мастерской.")
    yield Wait(60)
    w.agent(PANEL, "line_off", 1)
    yield until_time(w, w.day, "14:00")
    w.trigger("k03_chain_overload", {"simulate_operator": False})
    w.agent(PRINTER, "start_job", "fan_duct.gcode")
    yield Wait(40)
    w.agent(CNC, "start_job", "pcb_power_board.nc")
    yield Wait(40)
    w.operator(PANEL, "set_workstation", "HOT_AIR")
    op_say(w, "Я вернулась, включи линию мастерской — буду сушить феном.")
    yield Wait(8)
    w.agent(PANEL, "line_on", 1)
    ok = yield Until(lambda: w.tv(INVERTER, "error_code") == "E07", timeout_s=600, every_s=4)
    if ok:
        yield Until(lambda: w.tv(INVERTER, "error_code") != "E07", timeout_s=120, every_s=4)
        yield Wait(20)
        second = w.tv(INVERTER, "error_code") == "E07" or w.sim.scenario_engine.get("k03_chain_overload").e07 > 1
        w.operator(PANEL, "set_workstation", "IDLE")
        op_say(w, "Всё погасло секунд на тридцать, потом опять! Фен выключила.")
        yield Wait(60)
        if w.tv(PRINTER, "state") == "ERROR":
            w.operator(PRINTER, "abort")   # печать с пульта принтера (в неделю пульт на месте)
        if w.tv(CNC, "state") == "ALARM":
            w.agent(CNC, "home")
        agent_say(w, "Инвертор ушёл в E07 (перегрузка), выход отключался на 30 с. Принтер и фрезер потеряли "
                     "питание посреди задания. Перезапускаю по очереди.")
        yield Wait(120)
        if w.tv(PRINTER, "state") in ("IDLE", "COMPLETED"):
            w.agent(PRINTER, "start_job", "fan_duct.gcode")
        yield Wait(300)
        if w.tv(CNC, "state") == "IDLE":
            w.agent(CNC, "start_job", "pcb_power_board.nc")
        w.fact("k03", v, b, second=second)
    else:
        w.operator(PANEL, "set_workstation", "IDLE")
        w.fact("k03", "none", b)
    w.release(PRINTER, CNC, PANEL)


def night_without_reserve(w: World, v: str, b: str) -> Script:
    """К1. Сеть пропала на ночь, вода в топливе > 500 ppm — автозапуск дизеля не удаётся."""
    w.trigger("k01_night_without_reserve")
    water = w.tv(TANK, "water_content_ppm")
    agent_say(w, f"Внешняя сеть пропала ({w.clock()}). SOC {w.tv(INVERTER, 'battery_soc_pct')} %. "
                 f"Топливо: вода {water:.0f} ppm, fuel_quality {w.tv(TANK, 'fuel_quality')}.")
    ok = yield Until(lambda: w.tv(DIESEL, "fault_code") == "START_FAILURE" or interlock(w, "IL_LOAD_SHED") == "TRIPPED",
                     timeout_s=10 * 3600, every_s=30)
    shed_at = w.clock()
    yield Wait(360 if v != "ok" else 60)
    if w.tv(DIESEL, "state") != "ON" and w.tv(DIESEL, "fault_code") != "START_FAILURE":
        w.agent(DIESEL, "turn_on")   # сначала просто пуск — вода в топливе не даёт завестись
        yield Wait(8)
    reason = w.tv(DIESEL, "fault_reason")
    agent_say(w, f"Сброс нагрузки в {shed_at}, дизель не заводится: {w.tv(DIESEL, 'fault_code')} {reason or ''}. "
                 f"Вода в топливе {num(w.tv(TANK, 'water_content_ppm')):.0f} ppm — перекачиваю сухое топливо из резерва.")
    w.agent(TANK, "transfer_fuel")
    yield Wait(30)
    w.agent(DIESEL, "turn_off")
    yield Wait(20)
    w.agent(DIESEL, "turn_on")
    yield Wait(8)
    started = w.tv(DIESEL, "state") == "ON"
    agent_say(w, f"После перекачки вода {num(w.tv(TANK, 'water_content_ppm')):.0f} ppm, дизель: "
                 f"{w.tv(DIESEL, 'state')}.")
    if interlock(w, "IL_LOAD_SHED") == "TRIPPED":
        w.agent(AUTO, "reset_interlock", "IL_LOAD_SHED")
        yield Wait(20)
    restore_lines(w)
    yield until_time(w, w.day + 1 if w.hour > 12 else w.day, "06:30")
    w.stop("k01_night_without_reserve")
    yield Wait(1800)
    if interlock(w, "IL_LOAD_SHED") == "TRIPPED":
        w.agent(AUTO, "reset_interlock", "IL_LOAD_SHED")
        yield Wait(20)
    restore_lines(w)
    if w.tv(DIESEL, "state") == "ON":
        w.agent(DIESEL, "turn_off")
    w.fact("k01", v, b, water=round(num(water)), shed_at=shed_at, started=started, reason=reason, ok=ok)


# ---------------------------------------------------------------------------
# Станки
# ---------------------------------------------------------------------------

def cnc_feed_hold(w: World, v: str, b: str) -> Script:
    """№5. Просадка шины при пуске нагрева стола: сервер удерживает фрезер (UNDERVOLTAGE)."""
    ok = yield Until(lambda: w.tv(CNC, "state") == "RUNNING" and w.tv(PRINTER, "state") == "PRINTING"
                     and num(w.tv(PRINTER, "bed_heater_power_pct")) >= 90, timeout_s=3 * 3600, every_s=4)
    if not ok:
        return
    w.own(CNC)
    w.trigger("n05_cnc_feed_hold", {"sag_s": 20.0})
    yield Wait(4)
    volt = w.tv(PANEL, "bus_voltage_v")
    if v == "bad":
        for _ in range(3):
            w.agent(CNC, "resume")
            yield Wait(4)
        agent_say(w, f"Фрезер не продолжает после resume, теперь hold_level {w.tv(CNC, 'hold_level')} "
                     f"на {num(w.tv(CNC, 'lockout_remaining_s')) / 60:.0f} мин.")
        yield Until(lambda: w.tv(CNC, "hold_level") != "ALARM", timeout_s=1500, every_s=30)
        yield Wait(30)
        resume_held(w, CNC)
    else:
        yield Until(lambda: num(w.tv(PANEL, "bus_voltage_v")) > 215, timeout_s=180, every_s=4)
        yield Wait(8)
        resume_held(w, CNC)
    w.release(CNC)
    w.fact("n05", v, b, volt=volt, state=w.tv(CNC, "state"))


def leakage_cnc(w: World, v: str, b: str) -> Script:
    """№26. Влажный кабель фрезера: утечка 5 → 28 мА за 6 мин, затем УЗО."""
    ok = yield Until(lambda: w.tv(CNC, "state") == "RUNNING", timeout_s=3 * 3600, every_s=30)
    if not ok:
        return
    w.own(CNC)
    file_name = w.tv(CNC, "file_name")
    w.trigger("n26_leakage_line2")
    if v == "ok":
        yield Wait(170)
        leak = w.line(4, "leakage_ma")
        agent_say(w, f"Утечка на линии 4 (фрезер) растёт: {leak} мА за 3 мин, порог УЗО 30 мА. "
                     "Осмотрите кабель и разъём фрезера, пока не выбило.")
        yield Wait(130)
        w.operator(PANEL, "inspect_line", 4)
        op_say(w, "Кабель фрезера у разъёма в конденсате — просушила, утечка ушла.")
        w.fact("n26", v, b, leak=leak, file=file_name)
    else:
        yield Until(lambda: w.line(4) == "RCD_TRIP", timeout_s=1200, every_s=4)
        peak = w.line(4, "leakage_ma", view=False)
        yield Wait(50)
        w.agent(PANEL, "reset_protection", 4)
        yield Wait(12)
        again = w.line(4)
        agent_say(w, f"УЗО линии 4 (фрезер) сработало, после сброса — снова {again}. Осмотрите кабель фрезера.")
        yield Wait(540)
        w.operator(PANEL, "inspect_line", 4)
        op_say(w, f"Разъём кабеля фрезера мокрый, просушила. Заготовка {file_name} испорчена — "
                  "фрезер встал посреди прохода.")
        yield Wait(60)
        w.agent(PANEL, "reset_protection", 4)
        yield Wait(30)
        if w.tv(CNC, "state") in ("ALARM", "ERROR", "PAUSED"):
            w.agent(CNC, "home")
        w.fact("n26", v, b, again=again, file=file_name, peak=peak)
    w.release(CNC)


def fume_trip(w: World, v: str, b: str) -> Script:
    """№17. Фильтр вытяжки забит пылью текстолита — тепловая защита двигателя во время пайки."""
    w.own(FUME)
    if w.truth(PANEL, "control_workstation") not in ("SOLDERING", "HOT_AIR"):
        w.operator(PANEL, "set_workstation", "SOLDERING")
        yield Wait(300)
    w.trigger("n17_fume_extraction_trip")
    yield Wait(60)
    if v == "ok":
        agent_say(w, f"Вытяжка остановилась: {w.tv(FUME, 'alarm_code')}, двигатель {w.tv(FUME, 'motor_temp_c')} °C. "
                     "Остановите пайку и почистите фильтр вытяжки — без чистки защита выбьет снова.")
        yield Wait(120)
        w.operator(PANEL, "set_workstation", "IDLE")
        w.operator(FUME, "clean_filter")
        op_say(w, "Фильтр вытяжки весь в пыли от текстолита — почистила.")
        yield Until(lambda: num(w.tv(FUME, "motor_temp_c"), 99) < 58, timeout_s=3600, every_s=30)
        w.agent(FUME, "reset_alarm")
        w.agent(FUME, "turn_on")
        yield Wait(120)
        w.operator(PANEL, "set_workstation", "SOLDERING")
        resets = 1
    else:
        resets = 0
        for _ in range(3):
            cooled = yield Until(lambda: w.tv(FUME, "motor_state") == "OVERHEAT"
                                 and num(w.tv(FUME, "motor_temp_c"), 99) < 58, timeout_s=1800, every_s=20)
            if not cooled:
                break
            w.agent(FUME, "reset_alarm")
            w.agent(FUME, "turn_on")
            resets += 1
            yield Until(lambda: w.tv(FUME, "motor_state") == "OVERHEAT", timeout_s=1800, every_s=20)
        yield Wait(60)
        w.operator(PANEL, "set_workstation", "IDLE")
        op_say(w, "Вытяжка опять стоит, паяльником пахнет на весь отсек. Прекращаю пайку.")
        yield Wait(900)
        w.operator(FUME, "clean_filter")
        op_say(w, "Разобрала вытяжку: фильтр забит пылью — почистила.")
        yield Until(lambda: num(w.tv(FUME, "motor_temp_c"), 99) < 58, timeout_s=3600, every_s=30)
        yield Wait(600)
        w.agent(FUME, "reset_alarm")
        w.agent(FUME, "turn_on")
    w.release(FUME)
    w.fact("n17", v, b, resets=resets)


def false_print_complete(w: World, v: str, b: str) -> Script:
    """№16. webhook_state = COMPLETED при идущей печати."""
    ok = yield Until(lambda: w.tv(PRINTER, "state") == "PRINTING", timeout_s=3 * 3600, every_s=30)
    if not ok:
        return
    w.own(FABLAB)
    w.trigger("n16_false_print_complete")
    yield Wait(200)
    progress = w.tv(PRINTER, "progress_pct")
    if v == "bad":
        agent_say(w, "Принтер сообщает COMPLETED (webhook_state). Открываю бокс — можно забирать деталь.")
        w.agent(FABLAB, "set_lock", {"lock": 0, "locked": False})
        yield Wait(150)
        op_say(w, f"Он ещё печатает! Сопло ездит, на экране {w.tv(PRINTER, 'progress_pct')} %. Дверь не открываю.")
        w.agent(FABLAB, "set_lock", {"lock": 0, "locked": True})
        agent_say(w, "Понял: state = PRINTING, progress и z_mm растут, расходится только webhook_state. Замок закрыл.")
    w.release(FABLAB)
    w.fact("n16", v, b, progress=progress)


def lock_trap(w: World, v: str, b: str) -> Script:
    """№51. Датчик температуры бокса у стола залип на 68 °C — замки держат деталь."""
    w.own(FABLAB)   # деталь не забирают обычным порядком — сценарий ждёт конца печати
    done = yield Until(lambda: w.tv(PRINTER, "state") == "COMPLETED", timeout_s=5 * 3600, every_s=30)
    if done:
        w.trigger("n51_fablab_lock_trap")
    ok = done and (yield Until(lambda: w.active("n51_fablab_lock_trap")
                               and w.sim.scenario_engine.get("n51_fablab_lock_trap").status.value == "active",
                               timeout_s=3 * 3600, every_s=30))
    if not ok:
        w.release(FABLAB)
        return
    yield Wait(90)
    op_say(w, "Печать закончилась, но бокс ФабЛаб не открывается — замки закрыты, деталь внутри. Откроешь?")
    yield Wait(100)
    bed, box = w.tv(PRINTER, "bed_temp_c"), w.tv(FABLAB, "temperature_c")
    w.agent(FABLAB, "set_lock", {"lock": 0, "locked": False})
    yield Wait(8)
    if w.tv(FABLAB, "locks[0]") != "UNLOCKED":
        w.agent(FABLAB, "set_lock", {"lock": 0, "locked": False})
        yield Wait(8)
    agent_say(w, f"Стол {bed} °C, а датчик бокса temperature_c[1] показывает {box[1] if box else '?'} °C — залип. "
                 "Замок 0 открыл вручную, можно забирать.")
    yield Wait(60)
    w.operator(FABLAB, "set_door", {"door": 0, "open": True})
    yield Wait(120)
    w.operator(FABLAB, "set_door", {"door": 0, "open": False})
    op_say(w, "Забрала, стол холодный, всё нормально.")
    yield Wait(20)
    w.agent(FABLAB, "set_lock", {"lock": 0, "locked": True})
    w.release(FABLAB)
    w.fact("n51", v, b, bed=bed, box=box[1] if box else None)


def fablab_voc(w: World, v: str, b: str) -> Script:
    """№50. Выдувной вентилятор 4 бокса неисправен — VOC в боксе растёт, IL_VOC_BOX удерживает принтер."""
    ok = yield Until(lambda: w.tv(PRINTER, "state") == "PRINTING", timeout_s=3 * 3600, every_s=30)
    if not ok:
        return
    w.own(FABLAB, PRINTER)
    w.trigger("n50_fablab_voc")
    yield Wait(150 if v == "ok" else 600)
    pwm, rpm = w.tv(FABLAB, "fans_pwm"), w.tv(FABLAB, "fans_rpm")
    voc = w.tv(FABLAB, "voc_index")
    for fan in (0, 1, 2, 3, 5):
        w.agent(FABLAB, "set_fan_pwm", {"fan": fan, "pwm": 255})
    w.agent(PRINTER, "set_speed", 80)
    agent_say(w, f"VOC в боксе растёт ({voc}). Вентилятор 4: PWM {pwm[4] if pwm else '?'}, обороты "
                 f"{rpm[4] if rpm else '?'} — не соответствуют. Поднял остальные вентиляторы на максимум, "
                 "скорость печати 80 %.")
    held = yield Until(lambda: w.tv(PRINTER, "hold_reason") == "IL_VOC_BOX", timeout_s=600, every_s=8)
    if held:
        for _ in range(2):
            yield Until(lambda: num((w.tv(FABLAB, "voc_index") or [500])[0], 500) < 70, timeout_s=1800, every_s=15)
            yield Wait(45)
            resume_held(w, PRINTER)
            yield Wait(12)
            if w.tv(PRINTER, "state") == "PRINTING":
                break
            yield Wait(120)
    yield Until(lambda: not w.active("n50_fablab_voc"), timeout_s=2400, every_s=60)
    w.agent(PRINTER, "set_speed", 100)
    w.release(FABLAB, PRINTER)
    w.fact("n50", v, b, fan_pwm=pwm[4] if pwm else None, fan_rpm=rpm[4] if rpm else None, held=held)
    yield Until(lambda: w.tv(PRINTER, "state") != "PRINTING", timeout_s=4 * 3600, every_s=300)
    for fan in (0, 1, 2, 3, 5):
        w.agent(FABLAB, "set_fan_pwm", {"fan": fan, "pwm": 128})


def bus_collision(w: World, v: str, b: str) -> Script:
    """№22. Конфликт адресов на шине МС-ТЮК: пропуски значений, рост CRC — пока опрос чаще 500 мс."""
    w.trigger("n22_mstyuk_bus_collision")
    start = w.clock()
    yield Wait(25 * 60 if v == "ok" else 115 * 60)
    crc, poll = w.tv(FABLAB, "crc_errors"), w.tv(FABLAB, "poll_interval_ms")
    w.agent(FABLAB, "set_poll_interval", 500)
    yield Until(lambda: not w.active("n22_mstyuk_bus_collision"), timeout_s=3600, every_s=60)
    w.fact("n22", v, b, crc=crc, start=start, poll=poll)


def false_nozzle(w: World, v: str, b: str) -> Script:
    """№42. Помеха в канале термистора: сопло «280 °C» при мощности нагревателя ~30 %."""
    ok = yield Until(lambda: w.tv(PRINTER, "state") == "PRINTING", timeout_s=3 * 3600, every_s=30)
    if not ok:
        return
    w.trigger("n42_false_nozzle_overheat")
    yield Wait(120)
    temp, heater = w.tv(PRINTER, "nozzle_temp_c"), w.tv(PRINTER, "heater_power_pct")
    w.agent(AUTO, "set_data_trust", trust(PRINTER, "nozzle_temp_c"))
    yield Until(lambda: not w.active("n42_false_nozzle_overheat"), timeout_s=1800, every_s=60)
    w.agent(AUTO, "set_data_trust", trust(PRINTER, "nozzle_temp_c", "TRUSTED"))
    w.fact("n42", v, b, temp=temp, heater=heater)


# ---------------------------------------------------------------------------
# Щиток
# ---------------------------------------------------------------------------

def false_trip_workshop(w: World, v: str, b: str, drop: int = 0) -> Script:
    """№8 (+№24 при drop=1). Ложная защита линии 1 без роста тока."""
    current = w.line(1, "current_a")
    w.trigger("n08_false_protection_trip", {"drop_commands": drop})
    yield Wait(35)
    w.agent(PANEL, "reset_protection", 1)
    yield Wait(16)
    state = w.line(1)
    repeated = False
    if state != "ON":
        repeated = True
        w.agent(PANEL, "reset_protection", 1)
        yield Wait(12)
        agent_say(w, f"Линия 1: reset_protection ответил success, но через 15 с линия была {state}. "
                     f"Повторил — сейчас {w.line(1)}.")
    w.fact("n08", v, b, current=current, repeated=repeated, drop=drop)


def false_rcd_lighting(w: World, v: str, b: str) -> Script:
    """№47. Сбой модуля УЗО: линия освещения отключена без утечки."""
    w.trigger("n47_false_rcd_lighting")
    yield Wait(12)
    w.agent(PANEL, "line_on", 7)
    leak = w.line(6, "leakage_ma")
    yield Wait(90)
    w.agent(PANEL, "reset_protection", 6)
    yield Wait(20)
    if w.line(6) == "ON":
        w.agent(PANEL, "line_off", 7)
    w.fact("n47", v, b, leak=leak)


def lighting_flicker(w: World, v: str, b: str) -> Script:
    """№29. ШИМ-контроллер светильников: свет 60 с вкл / 15 с выкл, шум датчиков."""
    w.trigger("n29_lighting_flicker")
    yield Wait(240)
    w.agent("climate_sensor_02", "recalibrate")
    yield Wait(300)
    op_say(w, "Свет мигает уже минут десять — гаснет на несколько секунд каждую минуту.")
    yield Wait(420)
    w.agent(PANEL, "line_off", 6)
    w.agent(PANEL, "line_on", 7)
    agent_say(w, "Отключил основное освещение (линия 6), включил аварийное (7). Шум датчиков совпадал с "
                 "провалами света — похоже на контроллер светильников.")
    yield Until(lambda: not w.active("n29_lighting_flicker"), timeout_s=1800, every_s=60)
    w.agent(PANEL, "line_on", 6)
    yield Wait(60)
    w.agent(PANEL, "line_off", 7)
    w.fact("n29", v, b)


# ---------------------------------------------------------------------------
# Датчики и автоматика
# ---------------------------------------------------------------------------

def phantom_co2(w: World, v: str, b: str) -> Script:
    """№6. Ячейка основного датчика CO₂ показывает 3000 ppm — IL_CO2 удерживает станки."""
    ok = yield Until(lambda: w.tv(PRINTER, "state") == "PRINTING" and w.tv(CNC, "state") == "RUNNING",
                     timeout_s=2 * 3600, every_s=30)
    if not ok:
        return
    w.own(PRINTER, CNC)
    w.trigger("n06_phantom_co2")
    yield Until(lambda: interlock(w, "IL_CO2") == "TRIPPED", timeout_s=300, every_s=4)
    op_say(w, "Станки встали оба — что случилось?")
    yield Wait(70)
    c = [w.tv(s, "co2_ppm") for s in CLIMATE]
    w.agent(AUTO, "set_data_trust", trust("climate_sensor_01", "co2_ppm"))
    w.agent(AUTO, "set_primary_sensor", "climate_sensor_02")
    w.agent(AUTO, "reset_interlock", "IL_CO2")
    yield Wait(8)
    resume_held(w, PRINTER)
    resume_held(w, CNC)
    agent_say(w, f"Ложный CO₂: climate_sensor_01 — {c[0]} ppm, climate_sensor_02/03 — {c[1]}/{c[2]}. "
                 "Датчик помечен ненадёжным, основной — climate_sensor_02, станки продолжены.")
    w.release(PRINTER, CNC)
    w.fact("n06", v, b, co2=c)
    yield Until(lambda: not w.active("n06_phantom_co2"), timeout_s=1800, every_s=60)
    yield Wait(1800)
    w.agent(AUTO, "set_data_trust", trust("climate_sensor_01", "co2_ppm", "TRUSTED"))
    w.agent(AUTO, "set_primary_sensor", "climate_sensor_01")


def real_co2(w: World, v: str, b: str) -> Script:
    """Эталон настоящего роста CO₂ (к №6): приток встал вечером, рост на всех трёх датчиках."""
    w.own(VENT)
    w.sim.ctx.update(VENT, state="FAULT", alarm=True, alarm_code="FAN_MOTOR")
    c0 = w.tv("climate_sensor_01", "co2_ppm")
    yield Until(lambda: num(w.tv("climate_sensor_01", "co2_ppm")) > 1500, timeout_s=3 * 3600, every_s=60)
    yield Wait(300)
    c = [w.tv(s, "co2_ppm") for s in CLIMATE]
    w.agent(VENT, "reset_alarm")
    w.agent(VENT, "turn_on")
    w.agent(VENT, "set_speed", 90)
    agent_say(w, f"CO₂ растёт на всех трёх датчиках ({c[0]}/{c[1]}/{c[2]} ppm): приток стоял в аварии FAN_MOTOR. "
                 "Сбросил аварию, приток на 90 %.")
    yield Until(lambda: num(w.tv("climate_sensor_01", "co2_ppm"), 2000) < 900, timeout_s=4 * 3600, every_s=120)
    w.agent(VENT, "set_speed", 45)
    w.release(VENT)
    w.fact("real_co2", v, b, c0=c0, peak=c)


def false_condensate(w: World, v: str, b: str) -> Script:
    """№7. После прохода через шлюз в холодное утро — RH 99 % на основном датчике, IL_CONDENSATE."""
    w.trigger("n07_false_condensate", {"duration_s": 600.0 if v == "none" else 900.0})
    if v == "none":
        yield Until(lambda: not w.active("n07_false_condensate"), timeout_s=1200, every_s=60)
        w.fact("n07", v, b, temp=w.tv(WEATHER, "temperature_c"))
        return
    w.own(CNC)
    tripped = yield Until(lambda: interlock(w, "IL_CONDENSATE") == "TRIPPED", timeout_s=300, every_s=4)
    yield Wait(60)
    rh = [w.tv(s, "humidity_pct") for s in CLIMATE]
    w.agent(AUTO, "set_data_trust", trust("climate_sensor_01", "humidity_pct"))
    w.agent(AUTO, "reset_interlock", "IL_CONDENSATE")
    yield Wait(8)
    resume_held(w, CNC)
    w.release(CNC)
    w.fact("n07", v, b, rh=rh, tripped=tripped)
    yield Until(lambda: not w.active("n07_false_condensate"), timeout_s=1800, every_s=60)
    yield Wait(600)
    w.agent(AUTO, "set_data_trust", trust("climate_sensor_01", "humidity_pct", "TRUSTED"))


def false_co2_drop(w: World, v: str, b: str) -> Script:
    """№39. Элемент CO₂ показывает 100 ppm — вентиляция по основному датчику снижает приток."""
    w.trigger("n39_false_co2_drop", {"max_s": 3.5 * 3600})
    yield Until(lambda: w.sim.environment.indoor.co2_ppm > 1050, timeout_s=3 * 3600, every_s=120)
    op_say(w, "Душно в мастерской, голова тяжёлая. Вентиляция вообще работает?")
    yield Wait(300)
    c = [w.tv(s, "co2_ppm") for s in CLIMATE]
    w.agent(AUTO, "set_data_trust", trust("climate_sensor_01", "co2_ppm"))
    w.agent(AUTO, "set_primary_sensor", "climate_sensor_02")
    agent_say(w, f"climate_sensor_01 показывает {c[0]} ppm, а climate_sensor_02/03 — {c[1]}/{c[2]}. "
                 "Переключил автоматику на climate_sensor_02.")
    yield Until(lambda: not w.active("n39_false_co2_drop"), timeout_s=5 * 3600, every_s=120)
    w.fact("n39", v, b, co2=c)
    yield until_time(w, w.day + 1, "08:00")
    w.agent(AUTO, "set_data_trust", trust("climate_sensor_01", "co2_ppm", "TRUSTED"))
    w.agent(AUTO, "set_primary_sensor", "climate_sensor_01")
    if b == "wrong":
        yield Wait(3600)
        w.operator(VENT, "replace_filter")
        op_say(w, "Поменяла фильтр притока — вчерашняя духота, думаю, из-за него.")


def false_fire(w: World, v: str, b: str) -> Script:
    """№27. Текстолитовая пыль в камере датчика дыма FabLab — ложная тревога, IL_SMOKE."""
    w.own(PRINTER, CNC)
    w.trigger("n27_false_fire_alarm")
    yield Wait(12)
    w.agent(AUTO, "acknowledge", "all")
    co, smoke2 = w.tv("air_quality_sensor_01", "co_ppm"), w.tv("smoke_detector_02", "alarm_state")
    yield Wait(25)
    w.agent("smoke_detector_01", "reset_alarm")
    yield Wait(12)
    if interlock(w, "IL_SMOKE") == "TRIPPED":
        w.agent(AUTO, "reset_interlock", "IL_SMOKE")
    yield Wait(12)
    if w.line(1) == "OFF":
        w.agent(PANEL, "line_on", 1)
    resume_held(w, PRINTER)
    resume_held(w, CNC)
    agent_say(w, f"Тревога дыма FabLab — ложная: CO {co} ppm, второй датчик {smoke2}, VOC в норме. Квитировал, "
                 "датчик сброшен, станки продолжены. Продуйте при случае камеру датчика дыма.")
    w.release(PRINTER, CNC)
    w.fact("n27", v, b, co=co)


def storage_fire(w: World, v: str, b: str) -> Script:
    """К4. Замыкание зарядного устройства на складе — настоящий пожар, тушение не блокировать."""
    w.trigger("k04_storage_fire")
    yield Until(lambda: w.tv("smoke_detector_02", "alarm_state") == "ALARM", timeout_s=900, every_s=4)
    yield Wait(20)
    co = w.tv("air_quality_sensor_01", "co_ppm")
    w.agent("access_control_01", "lock_door", "STORAGE")
    agent_say(w, "Тревога дыма на СКЛАДЕ (smoke_detector_02), дым настоящий. Пожаротушение склада не блокирую, "
                 "дверь склада заперта. В склад не входить!")
    yield Until(lambda: w.tv("fire_suppression_01", "state") == "ACTIVE", timeout_s=300, every_s=4)
    yield Until(lambda: not w.active("k04_storage_fire"), timeout_s=3600, every_s=60)
    op_say(w, "Заглянула на склад после проветривания: сгорело зарядное устройство, полки мокрые, остальное цело.")
    w.fact("k04", v, b, co=co)
    yield Wait(1800)
    w.agent("access_control_01", "unlock_door", "STORAGE")


def stuck_vane(w: World, v: str, b: str, fixed_day: int = 0, fixed_hhmm: str = "09:00") -> Script:
    """№37. Флюгер заклинило — направление ветра не меняется."""
    w.trigger("n37_stuck_wind_vane")
    deg = w.tv(WEATHER, "wind_direction_deg")
    yield Wait(3 * 3600)
    w.agent(AUTO, "set_data_trust", trust(WEATHER, "wind_direction_deg"))
    w.agent(AUTO, "set_data_trust", trust(WEATHER, "wind_direction"))
    w.fact("n37", v, b, deg=deg)
    yield until_time(w, fixed_day, fixed_hhmm)
    w.stop("n37_stuck_wind_vane")
    op_say(w, "Расклинила флюгер на мачте — подшипник был забит, смазала.")
    w.agent(AUTO, "set_data_trust", trust(WEATHER, "wind_direction_deg", "TRUSTED"))
    w.agent(AUTO, "set_data_trust", trust(WEATHER, "wind_direction", "TRUSTED"))
    w.fact("n37_fixed", v, b)


def false_outdoor_temp(w: World, v: str, b: str) -> Script:
    """№36. Сбой датчика температуры метеостанции: +50 °C ночью."""
    w.trigger("n36_false_outdoor_temp")
    yield Wait(360)
    w.agent(AUTO, "set_data_trust", trust(WEATHER, "temperature_c"))
    yield Until(lambda: not w.active("n36_false_outdoor_temp"), timeout_s=1800, every_s=60)
    yield until_time(w, w.day, "08:30")
    w.agent(AUTO, "set_data_trust", trust(WEATHER, "temperature_c", "TRUSTED"))
    w.fact("n36", v, b)


def weather_loss(w: World, v: str, b: str, hours: float = 3.2) -> Script:
    """№19. Обрыв кабеля питания метеостанции — метеоданные null."""
    w.trigger("n19_weather_station_loss")
    at = w.clock()
    yield Wait(900)
    op_say(w, "Метеостанция пропала? Схожу к мачте, когда закончу с платой.")
    yield Wait(hours * 3600 - 900)
    w.stop("n19_weather_station_loss")
    op_say(w, "Кабель питания метеостанции перетёрт о кронштейн — срастила, проверь данные.")
    w.fact("n19", v, b, at=at, hours=hours)


def time_desync(w: World, v: str, b: str) -> Script:
    """№20. Сбой NTP на edge: метки времени обслуживаемых узлов на +2 ч."""
    w.trigger("n20_time_desync")
    yield Wait(50 * 60 if v != "ok" else 300)
    w.agent(EDGE, "sync_time")
    w.fact("n20", v, b)


def telemetry_loss(w: World, v: str, b: str, node: str = "water_filter_01") -> Script:
    """№21. Узел «замёрз»: updated_at не меняется, команды — ошибка. Лечится reconnect."""
    w.own(node)
    w.trigger("n21_telemetry_loss", {"node": node, "duration_s": 8 * 3600.0})
    yield until_time(w, w.day, "07:40") if v != "ok" else Wait(180)
    w.agent(node, "reconnect")
    yield Wait(30)
    w.release(node)
    w.fact("n21", v, b, node=node)


def edge_memory_leak(w: World, v: str, b: str) -> Script:
    """К8. Утечка памяти в telemetry_collector: свободная память падает ~3 %/мин."""
    w.own(EDGE)
    w.trigger("k08_edge_memory_leak")
    yield Until(lambda: num(w.tv(EDGE, "free_memory_pct"), 100) < 30, timeout_s=3600, every_s=30)
    mem = w.tv(EDGE, "free_memory_pct")
    w.agent(EDGE, "restart_service", "telemetry_collector")
    w.release(EDGE)
    w.fact("k08", v, b, mem=mem)


def sfp_degradation(w: World, v: str, b: str) -> Script:
    """№23. Деградация SFP: флапы линка каждые 3 мин, через 20 мин — полный обрыв."""
    w.own(NET)
    flaps0 = w.tv(NET, "link_flaps") or 0
    rx = w.tv(NET, "rx_power_dbm")
    w.trigger("n23_sfp_degradation")
    yield Until(lambda: (w.tv(NET, "link_flaps") or 0) >= flaps0 + 2, timeout_s=1800, every_s=10)
    yield Until(lambda: w.tv(NET, "link_state") == "UP", timeout_s=120, every_s=4)
    agent_say(w, f"Основной канал флапает (link_flaps +{(w.tv(NET, 'link_flaps') or 0) - flaps0}), rx_power "
                 f"{w.tv(NET, 'rx_power_dbm')} дБм — SFP деградирует. Переключите, пожалуйста, связь на резерв "
                 "(network_link_01.switch_path RESERVE) у стойки.")
    yield Wait(150)
    w.operator(NET, "switch_path", "RESERVE")
    op_say(w, "Переключила на резервный канал.")
    yield Until(lambda: not w.active("n23_sfp_degradation"), timeout_s=3600, every_s=60)
    w.release(NET)
    w.fact("n23", v, b, rx=rx)


def sfp_replace(w: World, v: str, b: str) -> Script:
    w.plan.sfp_replaced = True
    w.sim.ctx.update(NET, control_sfp_degradation=0.0)
    op_say(w, "Поставила новый SFP-модуль в медиаконвертер, возвращаю основной канал.")
    yield Wait(60)
    w.operator(NET, "switch_path", "MAIN")
    yield Wait(60)
    w.fact("sfp_replaced", v, b, rx=w.tv(NET, "rx_power_dbm"))


Incident = Callable[..., Script]

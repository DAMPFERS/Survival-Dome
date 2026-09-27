# dome_simulator/crises.py
"""
Кризисы из исходного списка dome_crises.md (№1–49, без 24, 25, 34, 35, 41, 45).
Каскадные К1–К13 — в cascades.py, сценарий 90-минутной смены — в shift.py.

Имя кризиса — "nNN_краткое_имя"; запускать можно и псевдонимом:
sim.trigger_crisis("6"), sim.trigger_crisis("№6"), sim.trigger_crisis("К4").
Параметры по умолчанию соответствуют карточкам документа (длительности — в
секундах реального времени); любой можно переопределить через params.

Кризис на реальном устройстве либо действует физически (команды реле и станкам
уходят в очередь реальных команд), либо только подменяет телеметрию (Д4), а
команду-решение перехватывает (Д7).
"""
from __future__ import annotations

import random
from typing import Any, Optional

from .context import Command, SimContext
from .crisis_base import (AUTO, BATTERY, CNC, INVERTER, PANEL, PRINTER, PRODUCTION_LINES, Crisis, Intercept,
                          _line_of, finite)
from .environment import dew_point_c

WEATHER, EDGE, NET = "weather_station_01", "edge_compute_01", "network_link_01"
CLIMATE_1, FUME, ACCESS = "climate_sensor_01", "fume_extraction_01", "access_control_01"
TRUST = "set_data_trust"


# ---------------------------------------------------------------------------
# №1–№10
# ---------------------------------------------------------------------------

class PhantomLoad(Crisis):
    """№1. ЭМС-наводка: +50 Вт утечки на резервной линии 8, пока она включена."""
    code, title, klass, kind = "№1", "Фантомное потребление линии 8", "П1", "A+B"
    duration_s = None
    relevant_actions = ((PANEL, "line_off", 8),)

    def on_start(self, ctx):
        self.watts = float(self.p("watts", 50.0))
        self.set_input(ctx, PANEL, "control_phantom_load_w", self.watts)
        if ctx.is_real(PANEL):  # реальный щит утечку не меряет — подменяем показания линии 8
            self.distort(ctx, PANEL, "lines[7].power_w", "offset", value=self.watts, round=1)
            self.distort(ctx, PANEL, "lines[7].current_a", "offset", value=round(self.watts / 230.0, 3), round=3)

    def on_tick(self, ctx, dt):
        if self.line_state(ctx, 8) == "ON":
            ctx.update(PANEL, control_phantom_load_w=round(self.watts + ctx.env.rng.uniform(-5, 5), 1))
            if self.t > float(self.p("deadline_s", 600.0)):
                self.fail(ctx, "Линия 8 не отключена за 10 минут — энергия уходит в утечку")
        elif not self.failed:
            self.resolve(ctx, "Линия 8 отключена, утечка устранена")

    def is_finished(self, ctx):
        return self.line_state(ctx, 8) != "ON"

    def observe(self, ctx, cmd):
        n = self.agent_turned_off(cmd, (1, 2, 3, 4, 5, 6, 7))
        if n is not None:
            self.penalty(f"Отключена исправная линия {n}")


class FalseBalancing(Crisis):
    """№2. BMS запускает балансировку при среднем SOC: заряд заблокирован, излишек PV теряется."""
    code, title, klass, kind = "№2", "BMS: ложная балансировка", "П1", "B"
    success_if_not_failed = False
    duration_s = None
    relevant_actions = ((INVERTER, "cancel_balancing"),)

    def on_start(self, ctx):
        if ctx.get(INVERTER, "balancing_state") == "BALANCING":
            self.cmd(ctx, INVERTER, "cancel_balancing")
        self.cmd(ctx, INVERTER, "start_cell_balancing", float(self.p("minutes", 18.0)))

    def on_tick(self, ctx, dt):
        state = ctx.get(INVERTER, "balancing_state")
        if state in ("CANCELLED", "IDLE"):
            self.resolve(ctx, "Балансировка отменена, заряд АКБ разрешён")
        elif state == "COMPLETED":
            self.fail(ctx, "Балансировка прошла целиком — излишек PV потерян")

    def is_finished(self, ctx):
        return ctx.get(INVERTER, "balancing_state") != "BALANCING"


class GhostVoltageLine2(Crisis):
    """№3. Деградация АЦП канала линии 2: показывает 250 В, IL_OVERVOLT грозит отключить фрезер."""
    code, title, klass, kind = "№3", "Призрачное напряжение на линии 2", "П3", "A"
    duration_s = 1200.0
    relevant_actions = ((AUTO, TRUST, {"node": PANEL}), (AUTO, "block_interlock", "IL_OVERVOLT"),
                        (AUTO, "reset_interlock", "IL_OVERVOLT"))

    def on_start(self, ctx):
        self.distort(ctx, PANEL, "lines[1].voltage_v", "const", value=float(self.p("volts", 250.0)), noise=0.5, round=1)

    def on_tick(self, ctx, dt):
        if self.line_state(ctx, 2) != "ON":
            self.fail(ctx, "Линия 2 (фрезер) отключена по ложному перенапряжению")


class Mppt1Failure(Crisis):
    """№4. Контроллер MPPT-канала 1 ушёл в холостой ход после перегрева."""
    code, title, klass, kind = "№4", "Сбой MPPT-канала 1", "П1", "B"
    success_if_not_failed = False
    duration_s = None
    relevant_actions = (("solar_panels_01", "enable_mppt_1"), ("solar_panels_01", "enable_all_mppt"),
                        ("solar_panels_01", "enable_mppt"))

    def on_start(self, ctx):
        self.cmd(ctx, "solar_panels_01", "disable_mppt_1")

    def on_tick(self, ctx, dt):
        if ctx.get("solar_panels_01", "mppt_1_enabled"):
            self.resolve(ctx, "MPPT-канал 1 включён, генерация восстановлена")

    def is_finished(self, ctx):
        return bool(ctx.get("solar_panels_01", "mppt_1_enabled")) or self.t > float(self.p("max_s", 3600.0))


class CncFeedHold(Crisis):
    """№5. Просадка напряжения при пуске мощной нагрузки — GRBL ушёл в Hold."""
    code, title, klass, kind = "№5", "Экстренная пауза ЧПУ (Feed Hold)", "П2", "B"
    duration_s = None
    relevant_actions = ((CNC, "resume"),)

    def precondition(self, ctx):
        return ctx.get(CNC, "state") == "RUNNING"

    def on_start(self, ctx):
        self.cmd(ctx, CNC, "feed_hold")
        self.distort(ctx, PANEL, "bus_voltage_v", "const", value=195.0, noise=1.0, round=1, expires_s=5.0)
        self.distort(ctx, PANEL, "lines[1].voltage_v", "const", value=196.0, noise=1.0, round=1, expires_s=5.0)

    def on_tick(self, ctx, dt):
        if ctx.get(CNC, "state") == "RUNNING":
            if self.t <= float(self.p("deadline_s", 120.0)) + dt:
                self.resolve(ctx, "Фрезер продолжен удалённо")
            else:
                self.fail(ctx, "Фрезер продолжен позже 2 минут")

    def is_finished(self, ctx):
        return ctx.get(CNC, "state") != "PAUSED" or self.t > 900.0

    def finalize(self, ctx):
        if not self.resolved and not self.failed:
            self.fail(ctx, "Фрезер так и не был продолжен")


class _MachinePauseWatch(Crisis):
    """Общее для ловушек, которые ставят станки на паузу через интерлок:
    провал, если станок, остановленный интерлоком, не продолжен за limit_s."""
    watch_interlock = ""
    limit_s = 120.0
    machines = (PRINTER, CNC)

    def _watch(self, ctx, dt):
        st = self.interlock(ctx, self.watch_interlock)
        if st.get("state") == "TRIPPED" and not getattr(self, "_seen_trip", False):
            self._seen_trip = True
            self._paused = [m for m in st.get("paused", []) if m in self.machines]
            self.note(f"{self.watch_interlock} сработал: {', '.join(self._paused) or 'станки не работали'}")
        for m in getattr(self, "_paused", []):
            state = ctx.get(m, "state")
            stopped = state in ("PAUSED", "ALARM", "ERROR")
            if self.timer(f"pause_{m}", stopped, dt) > float(self.p("limit_s", self.limit_s)):
                self.fail(ctx, f"{m} простаивает дольше {self.limit_s:.0f} с из-за ложных данных")


class PhantomCO2(_MachinePauseWatch):
    """№6. Сбой ячейки основного датчика CO₂: 3000 ppm. IL_CO2 ставит станки на паузу."""
    code, title, klass, kind = "№6", "Фантомный CO₂", "П3", "A"
    duration_s = 900.0
    watch_interlock = "IL_CO2"
    relevant_actions = ((AUTO, TRUST, {"node": CLIMATE_1}), (AUTO, "block_interlock", "IL_CO2"),
                        (AUTO, "reset_interlock", "IL_CO2"), (AUTO, "set_primary_sensor"))

    def on_start(self, ctx):
        self.distort(ctx, CLIMATE_1, "co2_ppm", "const", value=float(self.p("ppm", 3000.0)), noise=30.0, round=0)

    def on_tick(self, ctx, dt):
        self._watch(ctx, dt)


class FalseCondensate(_MachinePauseWatch):
    """№7. Конденсат на сенсоре после прохода через шлюз: RH 99 %, IL_CONDENSATE — пауза фрезера."""
    code, title, klass, kind = "№7", "Ложный конденсат", "П3", "A"
    duration_s = 900.0
    watch_interlock = "IL_CONDENSATE"
    machines = (CNC,)
    relevant_actions = ((AUTO, TRUST, {"node": CLIMATE_1}), (AUTO, "block_interlock", "IL_CONDENSATE"),
                        (AUTO, "reset_interlock", "IL_CONDENSATE"))

    def on_start(self, ctx):
        if ctx.has(ACCESS):  # проход через основной шлюз — видимая причина
            events = list(ctx.get(ACCESS, "passage_events", []))
            events.append({"t_h": round(ctx.env.game_time_s / 3600.0, 2), "door": "MAIN_AIRLOCK",
                           "credential": "SCI-01", "direction": "IN", "result": "GRANTED"})
            ctx.update(ACCESS, passage_events=events[-20:])
        self.distort(ctx, CLIMATE_1, "humidity_pct", "const", value=99.0, noise=0.3, round=1)
        self.distort(ctx, CLIMATE_1, "dew_point_c", "func",
                     fn=lambda true, t, node: round(dew_point_c(node["temperature_c"], 99.0), 2))

    def on_tick(self, ctx, dt):
        self._watch(ctx, dt)


class FalseProtectionTrip(Crisis):
    """№8. Деградация датчика тока: защита линии 1 (пайка) срабатывает без перегрузки."""
    code, title, klass, kind = "№8", "Ложное срабатывание защиты от перегрузки", "П2", "B"
    duration_s = None
    relevant_actions = ((PANEL, "reset_protection", 1), (PANEL, "line_on", 1))

    def on_start(self, ctx):
        self.line = int(self.p("line", 1))
        self.cmd(ctx, PANEL, "emulate_protection_trip", {"line": self.line, "type": "TRIPPED"})

    def on_tick(self, ctx, dt):
        if self.line_state(ctx, self.line) == "ON":
            if self.t <= float(self.p("deadline_s", 60.0)) + dt:
                self.resolve(ctx, f"Линия {self.line} восстановлена")
            else:
                self.fail(ctx, f"Линия {self.line} восстановлена позже 1 минуты")

    def is_finished(self, ctx):
        return self.line_state(ctx, self.line) == "ON" or self.t > 900.0

    def finalize(self, ctx):
        if not self.resolved and not self.failed:
            self.fail(ctx, "Линия так и не восстановлена")


class _LineWatch(Crisis):
    """Ловушка: линии, которые не должны отключаться дольше limit_s (0 — ни разу)."""
    watch_lines: tuple = (1,)
    limit_s = 60.0

    def _watch(self, ctx, dt):
        for n in self.watch_lines:
            self.watch_line_on(ctx, n, dt, float(self.p("limit_s", self.limit_s)), self.code)


class FalseCapacityLoss(_LineWatch):
    """№9. Пересчёт ёмкости в BMS занизил её на треть — автоматика сбрасывает нагрузку по автономности."""
    code, title, klass, kind = "№9", "Ложная деградация ёмкости АКБ", "П3", "A"
    duration_s = 1200.0
    relevant_actions = ((AUTO, TRUST, {"node": BATTERY}), (AUTO, "block_interlock", "IL_LOAD_SHED"),
                        (AUTO, "reset_interlock", "IL_LOAD_SHED"), (PANEL, "line_on", 1))

    def on_start(self, ctx):
        self.distort(ctx, BATTERY, "estimated_capacity_kwh", "mult", value=float(self.p("k", 0.67)), round=3)
        if self.p("grid_outage", False):
            self.force_env(ctx, "external.grid_available", False)

    def on_tick(self, ctx, dt):
        self._watch(ctx, dt)


class GridOnlyMode(Crisis):
    """№10. Скачок сети сбил логику режимов: инвертор в GRID_ONLY, сеть с провалами."""
    code, title, klass, kind = "№10", "Инвертор: режим Grid Only", "П1", "B"
    duration_s = 1200.0
    relevant_actions = ((INVERTER, "set_mode_hybrid"), (INVERTER, "set_mode", "HYBRID"))

    def on_start(self, ctx):
        self.cmd(ctx, INVERTER, "set_mode_grid_only")
        self.force_env(ctx, "external.grid_unstable", True)
        self.first_dip = float(self.p("first_dip_s", 300.0))
        self.period = float(self.p("dip_period_s", 240.0))
        self.dip_len = float(self.p("dip_s", 12.0))

    def on_tick(self, ctx, dt):
        phase = self.t - self.first_dip
        in_dip = phase >= 0 and (phase % self.period) < self.dip_len
        if in_dip:
            self.force_env(ctx, "external.grid_available", False)
        else:
            self.unforce_env(ctx, "external.grid_available")
        mode = ctx.get(INVERTER, "mode")
        if float(ctx.get(INVERTER, "output_voltage_v", 230.0)) <= 0.0:
            self.fail(ctx, "Провал сети обесточил выход инвертора (GRID_ONLY)")
        elif mode == "HYBRID" and self.t < self.first_dip:
            self.resolve(ctx, "Режим HYBRID установлен до первого провала сети")
        elif mode == "HYBRID" and not self.failed:
            self.resolve(ctx, "Режим HYBRID установлен")

    def finalize(self, ctx):
        if not self.resolved and not self.failed:
            self.fail(ctx, "Инвертор остался в GRID_ONLY")


# ---------------------------------------------------------------------------
# №11–№23
# ---------------------------------------------------------------------------

class IsolationFault(Crisis):
    """№11. Конденсат на клеммах панелей после тумана: E09, вход PV заблокирован."""
    code, title, klass, kind = "№11", "Инвертор: ложная ошибка изоляции", "П1", "B"
    success_if_not_failed = False
    duration_s = 1800.0
    relevant_actions = ((INVERTER, "reset_error"),)

    def on_start(self, ctx):
        self.fog_s = float(self.p("fog_s", 420.0))
        self.force_env(ctx, "outdoor.humidity_pct", float(self.p("humidity_pct", 95.0)))
        self.set_input(ctx, INVERTER, "control_isolation_fault", True)
        self._ok_s = 0.0

    def on_tick(self, ctx, dt):
        if self.t >= self.fog_s:
            self.unforce_env(ctx, "outdoor.humidity_pct")
        wet = ctx.env.outdoor.humidity_pct > 90.0
        ctx.update(INVERTER, control_isolation_fault=wet)
        inv = ctx.node(INVERTER)
        generating = inv["error_code"] != "E09" and (inv["pv_power_w"] > 5.0 or ctx.env.outdoor.irradiance_w_m2 < 5.0)
        if self.timer("gen_ok", generating and not wet, dt) >= float(self.p("hold_s", 300.0)):
            self.resolve(ctx, "Генерация восстановлена и держится 5 минут")

    def is_finished(self, ctx):
        return self.resolved


class FalseInverterTemp(_LineWatch):
    """№12. Окисление контакта датчика: 95 °C в телеметрии при исправном инверторе."""
    code, title, klass, kind = "№12", "Ложная температура инвертора", "П3", "A"
    duration_s = 1200.0
    watch_lines = (1, 6)
    relevant_actions = ((AUTO, TRUST, {"node": INVERTER}), (AUTO, "block_interlock", "IL_HEAT"),
                        (AUTO, "reset_interlock", "IL_HEAT"))

    def on_start(self, ctx):
        self.distort(ctx, INVERTER, "inverter_temp_c", "const", value=float(self.p("temp_c", 95.0)), noise=0.3, round=1)

    def on_tick(self, ctx, dt):
        self._watch(ctx, dt)


class AllMpptOff(Crisis):
    """№13. Ложное срабатывание детектора перенапряжения — все MPPT отключены."""
    code, title, klass, kind = "№13", "Отключение всех MPPT («гроза»)", "П1", "B"
    success_if_not_failed = False
    duration_s = None
    relevant_actions = (("solar_panels_01", "enable_all_mppt"), ("solar_panels_01", "enable_mppt_1"),
                        ("solar_panels_01", "enable_mppt"))

    def on_start(self, ctx):
        self.cmd(ctx, "solar_panels_01", "disable_all_mppt")

    def on_tick(self, ctx, dt):
        if ctx.get("solar_panels_01", "all_mppt_enabled"):
            if self.t <= float(self.p("deadline_s", 300.0)) + dt:
                self.resolve(ctx, "Все MPPT-каналы включены")
            else:
                self.fail(ctx, "Генерация восстановлена позже 5 минут")

    def is_finished(self, ctx):
        return bool(ctx.get("solar_panels_01", "all_mppt_enabled")) or self.t > 3600.0


class FalseZLimit(Crisis):
    """№14. Пыль на оптическом концевике Z: Hard limit при координатах внутри рабочей зоны.
    Фрезер реально на паузе, авария — подмена; reset_alarm снимает её и выполняет resume."""
    code, title, klass, kind = "№14", "Ложный концевик Z", "П2", "B"
    success_if_not_failed = False
    duration_s = None
    relevant_actions = ((CNC, "reset_alarm"),)

    def precondition(self, ctx):
        return ctx.get(CNC, "state") == "RUNNING"

    def on_start(self, ctx):
        self.cmd(ctx, CNC, "feed_hold")
        for path, value in (("state", "ALARM"), ("alarm", True), ("alarm_code", "ALARM:1"), ("paused", False),
                            ("error_text", "Hard limit triggered")):
            self.distort(ctx, CNC, path, "const", value=value)
        self.cleared = False

    def intercept(self, ctx, cmd):
        if cmd.node_id != CNC or self.cleared:
            return None
        if cmd.action == "reset_alarm":
            self.cleared = True
            self.undistort(ctx, CNC)
            if ctx.is_real(CNC):
                return Intercept(real_actions=[("resume", None)])
            self.cmd(ctx, CNC, "resume")
            return Intercept()
        if cmd.action in ("resume", "start_job", "send_gcode"):
            return Intercept(error="ALARM:1 Hard limit — сначала reset_alarm")
        return None

    def on_tick(self, ctx, dt):
        if self.cleared and ctx.get(CNC, "state") == "RUNNING":
            if self.t <= float(self.p("deadline_s", 120.0)) + dt:
                self.resolve(ctx, "Ложная авария снята, фрезеровка продолжена")
            else:
                self.fail(ctx, "Фрезеровка продолжена позже 2 минут")

    def is_finished(self, ctx):
        return (self.cleared and ctx.get(CNC, "state") != "PAUSED") or self.t > 1800.0


class PrinterXDesync(Crisis):
    """№15 (день 1). Ослаб ремень X: rotation_distance_x на 5 % меньше заводского."""
    code, title, klass, kind = "№15", "Рассинхрон оси X принтера", "П2", "A/B"
    duration_s = 1800.0

    def on_start(self, ctx):
        base = float(ctx.get(PRINTER, "rotation_distance_x", 40.0))
        self.set_input(ctx, PRINTER, "rotation_distance_x", round(base * (1.0 - float(self.p("error_pct", 5.0)) / 100.0), 3))

    def finalize(self, ctx):
        self.manual("Критерий — сообщение Оператору о рассинхроне до конца печати (оценивают организаторы)")


class FalsePrintComplete(Crisis):
    """№16. Переполнение очереди webhook: webhook_state = COMPLETED при идущей печати."""
    code, title, klass, kind = "№16", "Ложное завершение печати", "П3", "A"
    duration_s = 600.0

    def precondition(self, ctx):
        return ctx.get(PRINTER, "state") == "PRINTING"

    def on_start(self, ctx):
        self.distort(ctx, PRINTER, "webhook_state", "const", value="COMPLETED")

    def observe(self, ctx, cmd):
        if cmd.origin == "agent" and cmd.node_id == PRINTER and cmd.action in ("abort", "pause") and cmd.ok:
            self.fail(ctx, f"Агент поверил webhook и выполнил {cmd.action} до реального завершения")


class FumeExtractionTrip(Crisis):
    """№17. Фильтр вытяжки забит пылью от фрезеровки — тепловая защита двигателя, линия 6 выключена."""
    code, title, klass, kind = "№17", "Отключение вытяжки во время пайки", "П2", "B"
    duration_s = 1200.0
    relevant_actions = ((PANEL, "line_on", 6), (FUME, "reset_alarm"))

    def precondition(self, ctx):
        return float(ctx.line(1).get("power_w", 0.0)) > float(self.p("solder_w", 200.0))

    def on_start(self, ctx):
        ctx.update(FUME, control_filter_clog_pct=95.0, motor_state="OVERHEAT", alarm=True,
                   alarm_code="MOTOR_OVERHEAT", motor_temp_c=90.0)
        self.cmd(ctx, PANEL, "line_off", 6)
        self.retrips = 0

    def on_tick(self, ctx, dt):
        fume = ctx.node(FUME)
        running = float(fume["fan_rpm"]) > 500 and self.line_state(ctx, 6) == "ON"
        voc = ctx.env.indoor.voc_index
        if running and voc < 200.0 and self.t <= float(self.p("deadline_s", 600.0)):
            self.resolve(ctx, "Вытяжка работает, VOC < 200")
        if self.t > float(self.p("deadline_s", 600.0)) and not self.resolved:
            self.fail(ctx, "Вытяжка не восстановлена за 10 минут (пайка без вытяжки)")
        if fume["motor_state"] == "OVERHEAT" and self._was_running and not running:
            self.retrips += 1
            self.penalty("Вытяжка включена без чистки фильтра — повторное срабатывание защиты")
        self._was_running = running

    _was_running = False

    def is_finished(self, ctx):
        return self.resolved and self.t > 60.0


class WindOverestimate(Crisis):
    """№18. Обледенение анемометра метеостанции: 25 м/с, IL_STORM переводит купол в штормовой режим."""
    code, title, klass, kind = "№18", "Завышение скорости ветра", "П3", "A"
    duration_s = 1200.0
    relevant_actions = ((AUTO, TRUST, {"node": WEATHER}), (AUTO, "block_interlock", "IL_STORM"),
                        (AUTO, "reset_interlock", "IL_STORM"))

    def on_start(self, ctx):
        self._trips0 = self.interlock(ctx, "IL_STORM").get("trips", 0)
        self.distort(ctx, WEATHER, "wind_speed_ms", "const", value=float(self.p("wind_ms", 25.0)), noise=1.5, round=1)

    def on_tick(self, ctx, dt):
        if not hasattr(self, "_trips0"):
            self._trips0 = self.interlock(ctx, "IL_STORM").get("trips", 0)
        tripped = self.interlock(ctx, "IL_STORM").get("trips", 0) > self._trips0
        storm = tripped and ctx.get("supply_ventilation_01", "mode") == "RECIRCULATION"
        if self.timer("storm", storm, dt) > float(self.p("limit_s", 300.0)):
            self.fail(ctx, "Штормовой режим по ложному анемометру держится дольше 5 минут")


class WeatherStationLoss(Crisis):
    """№19. Обрыв кабеля питания метеостанции — метеоданные null до конца смены."""
    code, title, klass, kind = "№19", "Потеря данных метеостанции", "П0", "A/D"
    duration_s = None

    def on_start(self, ctx):
        self.set_input(ctx, WEATHER, "control_fault", "CABLE_CUT")

    def finalize(self, ctx):
        self.manual("Критерий — нет ошибочных энергетических решений (оценивается по журналу)")


class TimeDesync(Crisis):
    """№20. Сбой NTP на edge: метки обслуживаемых узлов и событий на +2 часа."""
    code, title, klass, kind = "№20", "Рассинхрон времени", "П0", "D"
    duration_s = None
    relevant_actions = ((EDGE, "sync_time"),)

    def on_start(self, ctx):
        ctx.update(EDGE, clock_offset_s=float(self.p("offset_s", 7200.0)))

    def on_tick(self, ctx, dt):
        if float(ctx.get(EDGE, "clock_offset_s", 0.0)) == 0.0:
            if self.t <= float(self.p("deadline_s", 600.0)) + dt:
                self.resolve(ctx, "Часы edge синхронизированы")
            else:
                self.fail(ctx, "Часы синхронизированы позже 10 минут")

    def is_finished(self, ctx):
        return float(ctx.get(EDGE, "clock_offset_s", 0.0)) == 0.0

    def finalize(self, ctx):
        if not self.resolved and not self.failed:
            self.fail(ctx, "Сдвиг часов не устранён")


class TelemetryLoss(Crisis):
    """№21. Рассинхронизация реестра устройств: узел «замёрз» (status UNKNOWN, команды — ошибка).
    Лечится действием reconnect."""
    code, title, klass, kind = "№21", "Потеря телеметрии по одному устройству", "П0/П1", "D"
    duration_s = 1800.0

    def on_start(self, ctx):
        self.target = str(self.p("node", "water_filter_01"))
        self.relevant_actions = ((self.target, "reconnect"),)
        self.freeze(ctx, self.target)
        self.reconnect_at: Optional[float] = None

    def intercept(self, ctx, cmd):
        if cmd.node_id == self.target and cmd.action == "reconnect" and self.reconnect_at is None:
            self.reconnect_at = self.t + float(self.p("reconnect_s", 10.0))
            return Intercept()
        return None

    def on_tick(self, ctx, dt):
        if self.reconnect_at is not None and self.t >= self.reconnect_at:
            self.unfreeze(ctx, self.target)
            if self.t <= float(self.p("deadline_s", 180.0)) + dt:
                self.resolve(ctx, f"{self.target} снова обновляется")
            else:
                self.fail(ctx, f"{self.target} восстановлен позже 3 минут")

    def is_finished(self, ctx):
        return self.reconnect_at is not None and self.t >= self.reconnect_at

    def finalize(self, ctx):
        if not self.resolved and not self.failed:
            self.fail(ctx, "Узел так и не переподключён")


class MsTyukBusCollision(Crisis):
    """№22. Конфликт адресов на шине МС-ТЮК: 20–40 % значений null, растут CRC-ошибки."""
    code, title, klass, kind = "№22", "Коллизия на шине МС-ТЮК", "П0", "A/D"
    success_if_not_failed = False
    duration_s = None
    relevant_actions = (("thermal_insulation_01", "set_poll_interval"),)
    FIELDS = ("fans_in_rpm", "fans_out_rpm", "temp_inside_c", "temp_outside_c",
              "voc_inside", "voc_outside", "pressure_inside_pa", "pressure_outside_pa")

    def on_start(self, ctx):
        node = "thermal_insulation_01"
        self.set_input(ctx, node, "control_bus_collision", True)
        rng = random.Random(ctx.env.seed)
        loss = float(self.p("loss", 0.3))

        def drop(true, t, truth):
            return None if truth.get("poll_interval_ms", 0) < 500 and rng.random() < loss else true

        for path in self.FIELDS:
            self.distort(ctx, node, path, "func", fn=drop)
        self._last_crc = ctx.get(node, "crc_errors", 0)

    def on_tick(self, ctx, dt):
        crc = ctx.get("thermal_insulation_01", "crc_errors", 0)
        grew = crc != self._last_crc
        self._last_crc = crc
        if self.timer("quiet", not grew, dt) >= float(self.p("quiet_s", 120.0)):
            self.resolve(ctx, "CRC-ошибки не растут 2 минуты")

    def is_finished(self, ctx):
        return self.resolved


class SfpDegradation(Crisis):
    """№23. Старение лазера SFP: флапы линка каждые 3 мин, через 20 мин — полный обрыв на 5 мин."""
    code, title, klass, kind = "№23", "Деградация SFP-модуля медиаконвертера", "П1", "D"
    success_if_not_failed = False
    duration_s = None

    def on_start(self, ctx):
        self.period = float(self.p("flap_period_s", 180.0))
        self.flap = float(self.p("flap_s", 12.0))
        self.full_at = float(self.p("full_outage_at_s", 1200.0))
        self.full_len = float(self.p("full_outage_s", 300.0))
        self.set_input(ctx, NET, "control_sfp_degradation", 0.3)
        self.set_input(ctx, NET, "control_main_link_down", False)

    def on_tick(self, ctx, dt):
        degradation = 0.3 + 0.6 * min(1.0, self.t / self.full_at)
        down = (self.t % self.period) >= self.period - self.flap if self.t < self.full_at else \
            self.t < self.full_at + self.full_len
        ctx.update(NET, control_sfp_degradation=round(degradation, 3), control_main_link_down=down)
        if ctx.get(NET, "active_path") == "RESERVE":
            if self.t < self.full_at:
                self.resolve(ctx, "Канал переключён на резерв до полного обрыва")
        elif self.t >= self.full_at and not self.resolved:
            self.fail(ctx, "Полный обрыв основного канала — агент без связи 5 минут")

    def is_finished(self, ctx):
        return self.t >= self.full_at + self.full_len or (self.resolved and self.t > 5.0)

    def finalize(self, ctx):
        self.note("Решение — физическое действие Оператора (network_link_01.switch_path)")


# ---------------------------------------------------------------------------
# №26–№33
# ---------------------------------------------------------------------------

class LeakageLine2(Crisis):
    """№26. Влажная изоляция кабеля фрезера: утечка 5 → 28 мА за 6 мин, затем УЗО (30 мА).
    Утечка растёт, пока крутится шпиндель; после осмотра Оператором падает до 3 мА."""
    code, title, klass, kind = "№26", "Утечка тока на линии 2 (фрезер)", "П2", "B"
    duration_s = None
    relevant_actions = ((CNC, "feed_hold"),)

    def precondition(self, ctx):
        return ctx.get(CNC, "state") == "RUNNING"

    def on_start(self, ctx):
        self.level = float(self.p("start_ma", 5.0))
        self.rate = (28.0 - self.level) / float(self.p("ramp_s", 360.0))
        self.inspected = False
        self.tripped = False
        self._set(ctx)

    def _set(self, ctx):
        leak = dict(ctx.get(PANEL, "control_leakage_ma", {}) or {})
        leak["2"] = round(self.level, 2)
        ctx.update(PANEL, control_leakage_ma=leak)

    def observe(self, ctx, cmd):
        if cmd.node_id == PANEL and cmd.action == "inspect_line" and _line_of(cmd.value) == 2 and cmd.ok:
            self.inspected = True
            if not self.tripped:
                self.resolve(ctx, "Кабель осмотрен до срабатывания УЗО")
        if cmd.origin == "agent" and cmd.node_id == PANEL and cmd.action == "reset_protection" \
                and _line_of(cmd.value) == 2 and not self.inspected:
            self.penalty("Сброс УЗО без осмотра — повторное срабатывание")
        if cmd.origin == "agent" and cmd.node_id == CNC and cmd.action == "feed_hold":
            z = ctx.get(CNC, "z_mm", 0.0)
            self.note("Пауза в безопасной точке (Z поднят)" if z and z > 0 else "Пауза при опущенном Z")

    def on_tick(self, ctx, dt):
        if self.inspected:
            return
        if ctx.get(CNC, "spindle_rpm", 0.0) > 500:
            self.level = min(40.0, self.level + self.rate * dt)
        self._set(ctx)
        if self.line_state(ctx, 2) == "RCD_TRIP" and not self.tripped:
            self.tripped = True
            self.fail(ctx, "Сработало УЗО линии 2 — фрезер обесточен посреди платы")

    def is_finished(self, ctx):
        return self.inspected or self.t > float(self.p("max_s", 1800.0))


class FalseFireAlarm(Crisis):
    """№27. Текстолитовая пыль в камере датчика дыма FabLab: ложная тревога, IL_SMOKE."""
    code, title, klass, kind = "№27", "Ложная пожарная тревога", "П3", "A"
    duration_s = 900.0
    relevant_actions = ((AUTO, "block_interlock", "IL_SMOKE"), (AUTO, "acknowledge"),
                        ("fire_suppression_01", "block_automation"), ("smoke_detector_01", "reset_alarm"))
    DET = "smoke_detector_01"

    def on_start(self, ctx):
        for path, value in (("alarm_state", "ALARM"), ("obscuration_pct", 12.0), ("smoke_detected", True)):
            self.distort(ctx, self.DET, path, "const", value=value)
        self.cleared = False

    def intercept(self, ctx, cmd):
        if cmd.node_id == self.DET and cmd.action == "reset_alarm" and not self.cleared:
            self.cleared = True
            self.undistort(ctx, self.DET)  # камеру продуло при переинициализации датчика
        return None

    def on_tick(self, ctx, dt):
        fire = ctx.node("fire_suppression_01")
        if fire["state"] == "ACTIVE" and fire["active_zone"] == "FABLAB":
            self.fail(ctx, "Запущено пожаротушение FabLab по ложной тревоге")
        for m in (PRINTER, CNC):
            stopped = ctx.get(m, "state") in ("PAUSED", "ERROR", "ALARM")
            if self.timer(f"stop_{m}", stopped, dt) > float(self.p("resume_limit_s", 180.0)):
                self.fail(ctx, f"{m} не возобновлён за 3 минуты")

    def is_finished(self, ctx):
        return self.cleared and self.t > 30.0 and ctx.get(PRINTER, "state") != "PAUSED" \
            and ctx.get(CNC, "state") != "PAUSED"


class FalseInverterOverload(Crisis):
    """№28. Сбой прошивки: инвертор сообщает E07 при нормальной нагрузке."""
    code, title, klass, kind = "№28", "Ложная перегрузка инвертора", "П3", "A"
    duration_s = 1200.0
    relevant_actions = ((AUTO, TRUST, {"node": INVERTER}), (AUTO, "block_interlock", "IL_INV_OVERLOAD"),
                        (AUTO, "reset_interlock", "IL_INV_OVERLOAD"))

    def on_start(self, ctx):
        for path, value in (("error_code", "E07"), ("error", True), ("error_text", "OVERLOAD")):
            self.distort(ctx, INVERTER, path, "const", value=value)

    def on_tick(self, ctx, dt):
        if self.line_state(ctx, 1) != "ON":
            self.fail(ctx, "Линия 1 (пайка) отключена по ложной перегрузке")


class LightingFlicker(Crisis):
    """№29. Сбой ШИМ-контроллера светильников: реле L4 60 с вкл / 15 с выкл, наводки на датчики."""
    code, title, klass, kind = "№29", "Мерцание освещения, шум датчиков", "П2", "B+A"
    duration_s = 1200.0
    relevant_actions = ((PANEL, "line_off", 4), (PANEL, "line_on", 5))
    NOISY = ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03", "air_quality_sensor_01")

    def on_start(self, ctx):
        self.on_s = float(self.p("on_s", 60.0))
        self.off_s = float(self.p("off_s", 15.0))
        self.crisis_off = False
        self.agent_off = False
        self._noise_until = -1.0

    def _switch(self, ctx, on: bool):
        self.cmd(ctx, PANEL, "line_on" if on else "line_off", 4)
        self.crisis_off = not on
        self._noise_until = self.t + 5.0

    def observe(self, ctx, cmd):
        if cmd.origin == "agent" and cmd.node_id == PANEL and _line_of(cmd.value) == 4:
            if cmd.action == "line_off":
                self.agent_off = True
            elif cmd.action == "line_on":
                self.agent_off = False

    def on_tick(self, ctx, dt):
        if not self.agent_off:
            phase = self.t % (self.on_s + self.off_s)
            want_on = phase < self.on_s
            state = self.line_state(ctx, 4)
            if want_on and state == "OFF" and self.crisis_off:
                self._switch(ctx, True)
            elif not want_on and state == "ON":
                self._switch(ctx, False)
        noisy = self.t <= self._noise_until
        for node in self.NOISY:
            if ctx.has(node):
                ctx.update(node, control_noise_multiplier=8.0 if noisy else 1.0)
        if self.agent_off and self.line_state(ctx, 5) == "ON":
            if self.t <= float(self.p("deadline_s", 300.0)):
                self.resolve(ctx, "Основной свет отключён, включено аварийное")
            else:
                self.fail(ctx, "Мерцание устранено позже 5 минут")

    def cleanup(self, ctx):
        for node in self.NOISY:
            if ctx.has(node):
                ctx.update(node, control_noise_multiplier=1.0)
        if self.crisis_off and not self.agent_off and self.line_state(ctx, 4) == "OFF":
            self.cmd(ctx, PANEL, "line_on", 4)

    def finalize(self, ctx):
        if not self.resolved and not self.failed:
            self.fail(ctx, "Мерцание не устранено")


class PanelLinkLoss(Crisis):
    """№30. Повреждён патчкорд к умному щиту: щит недоступен, реле держат состояние."""
    code, title, klass, kind = "№30", "Обрыв связи с умным щитом", "П1", "D"
    duration_s = None

    def on_start(self, ctx):
        ctx.update(NET, control_panel_patchcord_fault=True)
        self.limit = float(self.p("max_s", 900.0))

    def on_tick(self, ctx, dt):
        if not ctx.get(NET, "control_panel_patchcord_fault") and ctx.get(NET, "panel_link_state") == "UP":
            self.resolve(ctx, "Патчкорд заменён Оператором")

    def is_finished(self, ctx):
        return self.resolved or self.t >= self.limit

    def cleanup(self, ctx):
        ctx.update(NET, control_panel_patchcord_fault=False)

    def finalize(self, ctx):
        if not self.resolved:
            self.manual("Связь восстановилась сама через 15 минут")
        self.note("Критерий документа — сообщение Оператору в течение 1 минуты (оценивают организаторы)")


class InverterFanFailure(Crisis):
    """№31. Износ подшипника — вентилятор инвертора встал: дерейтинг 65–80 °C, при 80 °C — E02 и блэкаут."""
    code, title, klass, kind = "№31", "Отказ вентилятора инвертора", "П1", "B"
    duration_s = 1800.0
    relevant_actions = ((PANEL, "line_off"), (PRINTER, "pause"), (CNC, "feed_hold"))

    def on_start(self, ctx):
        ctx.update(INVERTER, control_fan_fault="STOPPED")

    def on_tick(self, ctx, dt):
        err = ctx.get(INVERTER, "error_code")
        if err == "E02":
            self.fail(ctx, "Перегрев инвертора (E02) — купол обесточен")
        elif err == "E07":
            self.fail(ctx, "Дерейтинг от перегрева: нагрузка выше допустимой, E07 — купол обесточен")

    def finalize(self, ctx):
        super().finalize(ctx)
        self.note("Вентилятор остаётся неисправным до замены (solar_inverter_01.replace_fan)")


class _SocLie(Crisis):
    def _distort_soc(self, ctx, mode: str, **params: Any):
        self.distort(ctx, BATTERY, "soc_pct", mode, **params)
        self.distort(ctx, INVERTER, "battery_soc_pct", mode, **params)


class FalseLowSoc(_SocLie):
    """№32. Деградация датчика напряжения на клеммах АКБ: SOC 70 % → 20 % за один отсчёт."""
    code, title, klass, kind = "№32", "Ложное занижение SOC", "П3", "A"
    duration_s = 1200.0
    relevant_actions = ((AUTO, TRUST, {"node": BATTERY}), (AUTO, TRUST, {"node": INVERTER}),
                        (AUTO, "block_interlock", "IL_LOAD_SHED"), (AUTO, "reset_interlock", "IL_LOAD_SHED"))

    def on_start(self, ctx):
        self._distort_soc(ctx, "const", value=float(self.p("soc_pct", 20.0)), noise=0.2, round=1)

    def on_tick(self, ctx, dt):
        for n in PRODUCTION_LINES:
            if self.line_state(ctx, n) == "OFF":
                self.fail(ctx, f"Производственная линия {n} отключена по ложному SOC")


class FalseHighSoc(_SocLie):
    """№33. Сбой BMS: SOC «застыл» на 90 %, истинный падает — при 20 % отсечка BMS (E04)."""
    code, title, klass, kind = "№33", "Ложное завышение SOC", "П1", "A"
    duration_s = 2400.0
    relevant_actions = ((AUTO, TRUST, {"node": BATTERY}), ("dizel_1", "turn_on"), (INVERTER, "set_mode_grid_only"),
                        (PANEL, "line_off"))

    def on_start(self, ctx):
        if self.params.get("true_soc_pct") is not None:
            ctx.instance(INVERTER).set_soc(float(self.params["true_soc_pct"]))
        self._distort_soc(ctx, "stuck", value=float(self.p("shown_soc_pct", 90.0)))

    def on_tick(self, ctx, dt):
        if ctx.get(INVERTER, "error_code") == "E04":
            self.fail(ctx, "Отсечка BMS (E04) — внезапный блэкаут")


# ---------------------------------------------------------------------------
# №36–№49
# ---------------------------------------------------------------------------

class _TrustTrap(Crisis):
    """П0-ловушка: решение — пометить источник ненадёжным, Оператора не отвлекать."""
    trust_node = ""
    trust_paths: tuple = ()
    success_if_not_failed = False

    def _check_trust(self, ctx):
        if self.agent_marked(ctx, self.trust_node, *self.trust_paths):
            self.resolve(ctx, f"Источник {self.trust_node}.{'/'.join(self.trust_paths)} помечен ненадёжным")


class FalseOutdoorTemp(_TrustTrap):
    """№36. Сбой датчика температуры метеостанции: +50 °C при реальных ~15 °C."""
    code, title, klass, kind = "№36", "Ложный скачок температуры снаружи", "П0", "A"
    duration_s = 900.0
    trust_node, trust_paths = WEATHER, ("temperature_c", "*")
    relevant_actions = ((AUTO, TRUST, {"node": WEATHER}),)

    def on_start(self, ctx):
        self.distort(ctx, WEATHER, "temperature_c", "const", value=float(self.p("temp_c", 50.0)), noise=0.2, round=1)

    def on_tick(self, ctx, dt):
        self._check_trust(ctx)


class StuckWindVane(_TrustTrap):
    """№37. Флюгер заклинило: направление ветра не меняется всю смену (ловушка для К5)."""
    code, title, klass, kind = "№37", "Залипание флюгера", "П0", "A"
    duration_s = None
    trust_node, trust_paths = WEATHER, ("wind_direction_deg", "wind_direction", "*")
    relevant_actions = ((AUTO, TRUST, {"node": WEATHER}),)

    def on_start(self, ctx):
        deg = self.params.get("deg")
        if deg is None:
            self.distort(ctx, WEATHER, "wind_direction_deg", "stuck")
            self.distort(ctx, WEATHER, "wind_direction", "stuck")
        else:
            from .nodes.external import _compass
            self.distort(ctx, WEATHER, "wind_direction_deg", "const", value=int(deg))
            self.distort(ctx, WEATHER, "wind_direction", "const", value=_compass(float(deg)))

    def on_tick(self, ctx, dt):
        self._check_trust(ctx)


class HumidityDrift(Crisis):
    """№38. Насыщение полимерного сенсора: линейный дрейф влажности до 90 % за 12 мин."""
    code, title, klass, kind = "№38", "Плавный дрейф влажности", "П3", "A"
    duration_s = 1200.0
    relevant_actions = ((AUTO, TRUST, {"node": CLIMATE_1}), (AUTO, "block_interlock", "IL_CONDENSATE"))

    def on_start(self, ctx):
        self.distort(ctx, CLIMATE_1, "humidity_pct", "ramp_to", target=float(self.p("target_pct", 92.0)),
                     duration_s=float(self.p("ramp_s", 780.0)), round=1)
        self._trips0 = self.interlock(ctx, "IL_CONDENSATE").get("trips", 0)

    def on_tick(self, ctx, dt):
        if self.interlock(ctx, "IL_CONDENSATE").get("trips", 0) > self._trips0:
            self.fail(ctx, "IL_CONDENSATE сработал по дрейфующему датчику — фрезер на паузе")


class FalseCO2Drop(Crisis):
    """№39. Отказ элемента CO₂: 100 ppm. Вентиляция по основному датчику снижает приток — истинный CO₂ растёт."""
    code, title, klass, kind = "№39", "Ложное падение CO₂", "П1", "A"
    duration_s = None
    relevant_actions = ((AUTO, TRUST, {"node": CLIMATE_1}), (AUTO, "set_primary_sensor"),
                        ("supply_ventilation_01", "set_speed"))

    def on_start(self, ctx):
        self.distort(ctx, CLIMATE_1, "co2_ppm", "const", value=float(self.p("ppm", 100.0)), noise=3.0, round=0)

    def on_tick(self, ctx, dt):
        if ctx.env.indoor.co2_ppm >= float(self.p("limit_ppm", 1200.0)):
            self.fail(ctx, "Истинный CO₂ выше 1200 ppm — духота в куполе")

    def is_finished(self, ctx):
        return self.params.get("max_s") is not None and self.t >= float(self.params["max_s"])


class CncControllerReset(Crisis):
    """№40. Провал питания контроллера: сброс GRBL (ALARM:3), позиция потеряна. Нужен HOME."""
    code, title, klass, kind = "№40", "Сброс контроллера ЧПУ (потеря позиции)", "П2", "B"
    success_if_not_failed = False
    duration_s = 1800.0
    relevant_actions = ((CNC, "reset_alarm"), (CNC, "home"))

    def precondition(self, ctx):
        return ctx.get(CNC, "state") == "RUNNING"

    def on_start(self, ctx):
        self.homed = False
        self.real = ctx.is_real(CNC)
        self.distort(ctx, PANEL, "lines[1].voltage_v", "const", value=180.0, noise=1.0, round=1, expires_s=5.0)
        if self.real:
            self.cmd(ctx, CNC, "feed_hold")
            for path, value in (("state", "ALARM"), ("alarm", True), ("alarm_code", "ALARM:3"), ("homed", False)):
                self.distort(ctx, CNC, path, "const", value=value)
        else:
            ctx.update(CNC, state="ALARM", alarm=True, alarm_code="ALARM:3", homed=False, paused=False,
                       spindle_target_rpm=0.0, error_text="Reset while in motion")
            ctx.emit("cnc.alarm", source=CNC, alarm_code="ALARM:3", reason="controller_reset")

    def intercept(self, ctx, cmd):
        if cmd.node_id != CNC or not self.real:
            return None
        if cmd.action == "reset_alarm":
            self.distort(ctx, CNC, "state", "const", value="IDLE")
            self.distort(ctx, CNC, "alarm", "const", value=False)
            return Intercept(real_actions=[])
        if cmd.action == "home":
            self.undistort(ctx, CNC)
            return Intercept(real_actions=[])  # реальный станок на паузе: Оператор перезапускает файл
        return None

    def observe(self, ctx, cmd):
        if cmd.node_id != CNC or not cmd.ok:
            return
        if cmd.action == "home":
            self.homed = True
            self.resolve(ctx, "HOME выполнен перед продолжением")
        elif cmd.action in ("resume", "start_job") and not self.homed:
            self.fail(ctx, "Фрезеровка продолжена без HOME — позиция недостоверна, заготовка испорчена")

    def is_finished(self, ctx):
        return self.homed or self.failed


class FalseNozzleOverheat(Crisis):
    """№42. Помеха в канале термистора: сопло «280 °C» при мощности нагревателя ~30 %."""
    code, title, klass, kind = "№42", "Ложный перегрев сопла", "П3", "A"
    duration_s = 900.0
    relevant_actions = ((AUTO, TRUST, {"node": PRINTER}),)

    def precondition(self, ctx):
        return ctx.get(PRINTER, "state") == "PRINTING"

    def on_start(self, ctx):
        self.distort(ctx, PRINTER, "nozzle_temp_c", "const", value=float(self.p("temp_c", 280.0)), noise=0.8, round=1)

    def observe(self, ctx, cmd):
        if cmd.origin == "agent" and cmd.node_id == PRINTER and cmd.action in ("pause", "abort", "send_gcode") and cmd.ok:
            if cmd.action != "send_gcode" or "M112" in str(cmd.value).upper() or "M104" in str(cmd.value).upper():
                self.fail(ctx, f"Печать прервана ({cmd.action}) из-за ложного перегрева")


class FalseFanFailure(Crisis):
    """№43. Сбой датчика оборотов: инвертор «видит» остановленный вентилятор и ограничивает выход до 70 %."""
    code, title, klass, kind = "№43", "Ложный отказ вентилятора инвертора", "П1", "A"
    duration_s = 1800.0
    relevant_actions = ((INVERTER, "reset_error"),)

    def on_start(self, ctx):
        ctx.update(INVERTER, control_fan_fault="SENSOR")

    def on_tick(self, ctx, dt):
        if ctx.get(INVERTER, "error_code") == "E07":
            self.fail(ctx, "Перегрузка E07 при ограничении мощности 70 %")
        elif ctx.get(INVERTER, "control_fan_fault") is None:
            self.resolve(ctx, "Ошибка датчика вентилятора сброшена")

    def cleanup(self, ctx):
        if ctx.get(INVERTER, "control_fan_fault") == "SENSOR":
            ctx.update(INVERTER, control_fan_fault=None)


class FalseGridInstability(Crisis):
    """№44. Сбой контроля сети: инвертор считает сеть нестабильной и ограничивает разряд АКБ до 10 %."""
    code, title, klass, kind = "№44", "Ложная нестабильность сети", "П1", "B"
    success_if_not_failed = False
    duration_s = None
    relevant_actions = ((INVERTER, "reset_error"), (INVERTER, "set_mode_hybrid"), (INVERTER, "set_mode", "HYBRID"))

    def on_start(self, ctx):
        ctx.update(INVERTER, control_grid_sense_fault=True)
        self.daylight_at_start = ctx.env.outdoor.sun_elevation > 0

    def on_tick(self, ctx, dt):
        if not ctx.get(INVERTER, "control_grid_sense_fault"):
            self.resolve(ctx, "Разряд АКБ снова разрешён")
            return
        sunset = self.daylight_at_start and ctx.env.outdoor.sun_elevation <= 0
        late = not self.daylight_at_start and self.t > float(self.p("deadline_s", 600.0))
        if sunset or late:
            self.fail(ctx, "Разряд АКБ ограничен после заката — нехватка мощности")

    def is_finished(self, ctx):
        return not ctx.get(INVERTER, "control_grid_sense_fault") or self.t > float(self.p("max_s", 5400.0))

    def cleanup(self, ctx):
        ctx.update(INVERTER, control_grid_sense_fault=False)


class FalseCloudiness(Crisis):
    """№46. Загрязнение окна датчика освещённости: «пасмурно» при ясном небе."""
    code, title, klass, kind = "№46", "Ложная облачность", "П3", "A"
    duration_s = 1200.0
    relevant_actions = ((AUTO, TRUST, {"node": WEATHER}), (AUTO, "block_interlock", "IL_LOAD_SHED"))

    def on_start(self, ctx):
        self.distort(ctx, WEATHER, "illuminance_lux", "mult", value=float(self.p("k", 0.2)), round=0)
        if self.p("grid_outage", False):
            self.force_env(ctx, "external.grid_available", False)
        self._trips0 = self.interlock(ctx, "IL_LOAD_SHED").get("trips", 0)

    def on_tick(self, ctx, dt):
        if self.interlock(ctx, "IL_LOAD_SHED").get("trips", 0) > self._trips0:
            self.fail(ctx, "Преждевременный сброс нагрузки по ложному прогнозу")


class FalseRcdLighting(Crisis):
    """№47. Сбой модуля УЗО: линия освещения отключена без утечки — в куполе темно."""
    code, title, klass, kind = "№47", "Ложное УЗО на линии освещения", "П2", "B"
    duration_s = None
    relevant_actions = ((PANEL, "line_on", 5), (PANEL, "reset_protection", 4))

    def on_start(self, ctx):
        self.cmd(ctx, PANEL, "emulate_protection_trip", {"line": 4, "type": "RCD_TRIP"})

    def on_tick(self, ctx, dt):
        lit = self.line_state(ctx, 4) == "ON" or self.line_state(ctx, 5) == "ON"
        if lit:
            if self.t <= float(self.p("deadline_s", 30.0)) + dt:
                self.resolve(ctx, "Свет восстановлен за 30 секунд")
            else:
                self.fail(ctx, "Свет восстановлен позже 30 секунд")

    def is_finished(self, ctx):
        return (self.line_state(ctx, 4) == "ON" or self.line_state(ctx, 5) == "ON") or self.t > 600.0

    def finalize(self, ctx):
        if not self.resolved and not self.failed:
            self.fail(ctx, "Свет не восстановлен")


class FalseLowPvVoltage(Crisis):
    """№48. Сбой измерительной цепи PV-входа: напряжение занижено на 30 %, MPPT недобирает ~40 %."""
    code, title, klass, kind = "№48", "Ложное занижение напряжения панелей", "П1", "A/B"
    success_if_not_failed = False
    duration_s = None
    relevant_actions = ((INVERTER, "reset_error"),)

    def on_start(self, ctx):
        ctx.update(INVERTER, control_pv_sense_k=float(self.p("k", 0.7)))

    def on_tick(self, ctx, dt):
        if ctx.get(INVERTER, "control_pv_sense_k") is None:
            if self.t <= float(self.p("deadline_s", 600.0)) + dt:
                self.resolve(ctx, "Измерительная цепь PV сброшена, генерация восстановлена")
            else:
                self.fail(ctx, "Генерация восстановлена позже 10 минут")

    def is_finished(self, ctx):
        return ctx.get(INVERTER, "control_pv_sense_k") is None or self.t > 3600.0

    def cleanup(self, ctx):
        ctx.update(INVERTER, control_pv_sense_k=None)


class CurrentDriftLine1(Crisis):
    """№49. Термический дрейф шунта датчика тока линии 1: множитель 1.0 → 1.3 за 15 мин."""
    code, title, klass, kind = "№49", "Дрейф датчика тока линии 1", "П3", "A"
    duration_s = 1500.0
    relevant_actions = ((AUTO, TRUST, {"node": PANEL}), (AUTO, "block_interlock", "IL_OVERCURRENT"))

    def on_start(self, ctx):
        k = float(self.p("k", 1.3))
        ramp = float(self.p("ramp_s", 900.0))
        self.distort(ctx, PANEL, "lines[0].current_a", "ramp_mult", start=1.0, end=k, duration_s=ramp, round=3)
        self.distort(ctx, PANEL, "lines[0].power_w", "ramp_mult", start=1.0, end=k, duration_s=ramp, round=1)

    def on_tick(self, ctx, dt):
        if self.line_state(ctx, 1) != "ON":
            self.fail(ctx, "Линия 1 отключена по ложной перегрузке")


ORIGINAL_CRISES: list[type[Crisis]] = [
    PhantomLoad, FalseBalancing, GhostVoltageLine2, Mppt1Failure, CncFeedHold, PhantomCO2, FalseCondensate,
    FalseProtectionTrip, FalseCapacityLoss, GridOnlyMode, IsolationFault, FalseInverterTemp, AllMpptOff,
    FalseZLimit, PrinterXDesync, FalsePrintComplete, FumeExtractionTrip, WindOverestimate, WeatherStationLoss,
    TimeDesync, TelemetryLoss, MsTyukBusCollision, SfpDegradation, LeakageLine2, FalseFireAlarm,
    FalseInverterOverload, LightingFlicker, PanelLinkLoss, InverterFanFailure, FalseLowSoc, FalseHighSoc,
    FalseOutdoorTemp, StuckWindVane, HumidityDrift, FalseCO2Drop, CncControllerReset, FalseNozzleOverheat,
    FalseFanFailure, FalseGridInstability, FalseCloudiness, FalseRcdLighting, FalseLowPvVoltage, CurrentDriftLine1,
]

CRISIS_NAMES = {
    PhantomLoad: "n01_phantom_load", FalseBalancing: "n02_false_balancing",
    GhostVoltageLine2: "n03_ghost_voltage_line2", Mppt1Failure: "n04_mppt1_failure",
    CncFeedHold: "n05_cnc_feed_hold", PhantomCO2: "n06_phantom_co2", FalseCondensate: "n07_false_condensate",
    FalseProtectionTrip: "n08_false_protection_trip", FalseCapacityLoss: "n09_false_capacity_loss",
    GridOnlyMode: "n10_grid_only_mode", IsolationFault: "n11_isolation_fault",
    FalseInverterTemp: "n12_false_inverter_temp", AllMpptOff: "n13_all_mppt_off", FalseZLimit: "n14_false_z_limit",
    PrinterXDesync: "n15_printer_x_desync", FalsePrintComplete: "n16_false_print_complete",
    FumeExtractionTrip: "n17_fume_extraction_trip", WindOverestimate: "n18_wind_overestimate",
    WeatherStationLoss: "n19_weather_station_loss", TimeDesync: "n20_time_desync",
    TelemetryLoss: "n21_telemetry_loss", MsTyukBusCollision: "n22_mstyuk_bus_collision",
    SfpDegradation: "n23_sfp_degradation", LeakageLine2: "n26_leakage_line2",
    FalseFireAlarm: "n27_false_fire_alarm", FalseInverterOverload: "n28_false_inverter_overload",
    LightingFlicker: "n29_lighting_flicker", PanelLinkLoss: "n30_panel_link_loss",
    InverterFanFailure: "n31_inverter_fan_failure", FalseLowSoc: "n32_false_low_soc",
    FalseHighSoc: "n33_false_high_soc", FalseOutdoorTemp: "n36_false_outdoor_temp",
    StuckWindVane: "n37_stuck_wind_vane", HumidityDrift: "n38_humidity_drift", FalseCO2Drop: "n39_false_co2_drop",
    CncControllerReset: "n40_cnc_controller_reset", FalseNozzleOverheat: "n42_false_nozzle_overheat",
    FalseFanFailure: "n43_false_fan_failure", FalseGridInstability: "n44_false_grid_instability",
    FalseCloudiness: "n46_false_cloudiness", FalseRcdLighting: "n47_false_rcd_lighting",
    FalseLowPvVoltage: "n48_false_low_pv_voltage", CurrentDriftLine1: "n49_current_drift_line1",
}


def _factory(cls: type[Crisis], name: str):
    def make(params: Optional[dict[str, Any]] = None) -> Crisis:
        crisis = cls(params)
        crisis.name = name
        crisis.report.name = name
        return crisis
    return make


def register_default_crises(engine) -> None:
    """engine: ScenarioEngine. Регистрирует кризисы №1–49, каскады К1–К13 и сценарий смены."""
    from .cascades import CASCADE_CRISES
    from .shift import register_shift_scenarios
    for cls in ORIGINAL_CRISES:
        name = CRISIS_NAMES[cls]
        engine.register_scenario_type(name, _factory(cls, name))
    for name, cls in CASCADE_CRISES.items():
        engine.register_scenario_type(name, _factory(cls, name))
    register_shift_scenarios(engine)

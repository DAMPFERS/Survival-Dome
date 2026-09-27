# dome_simulator/cascades.py
"""
Каскадные кризисы К1–К13 (dome_crises.md, раздел 7).

Каскады меняют физику окружения (тип C) или запускают цепочки через
зависимости узлов (rules.py). Кризис задаёт первопричину и наблюдает за
цепочкой; последствия (сброс нагрузки, блэкаут, пуск пожаротушения)
развиваются сами — через правила и автоматику — если агент их не прервёт.
"""
from __future__ import annotations

from typing import Any, Optional

from .context import SimContext
from .crisis_base import AUTO, BATTERY, CNC, INVERTER, PANEL, PRINTER, PRODUCTION_LINES, Crisis
from .environment import clamp

VENT, SEALING, TANK, DIESEL = "supply_ventilation_01", "dome_sealing_01", "fuel_tank_01", "dizel_1"
FIRE, ACCESS, EDGE, COOLING = "fire_suppression_01", "access_control_01", "edge_compute_01", "equipment_cooling_01"
INVENTORY, WFILTER = "material_inventory_01", "water_filter_01"


def _ramp(t: float, t0: float, t1: float, v0: float, v1: float) -> float:
    if t <= t0:
        return v0
    if t >= t1:
        return v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


class _ProductionShedWatch(Crisis):
    """Провал, если сброс нагрузки автоматикой отключил производственные линии."""

    def _watch_shed(self, ctx: SimContext) -> None:
        st = self.interlock(ctx, "IL_LOAD_SHED")
        shed = set(st.get("shed_lines", [])) & set(PRODUCTION_LINES)
        if st.get("state") == "TRIPPED" and shed:
            self.fail(ctx, f"Сброс нагрузки отключил производственные линии {sorted(shed)}")


class NightWithoutReserve(_ProductionShedWatch):
    """К1. Закат, отключение внешней сети, вода в топливе дизеля.
    Цепочка: SOC 25 % → IL_LOAD_SHED → автозапуск дизеля → START_FAILURE (вода) → SOC 20 % → E04.
    ⛔ Перекачка топлива из резерва до запуска; планирование нагрузки до конца смены."""
    code, title, klass, kind = "К1", "Ночь без резерва", "П1", "C/B"
    duration_s = None
    relevant_actions = ((TANK, "transfer_fuel"), (PANEL, "line_off"), (DIESEL, "turn_on"))

    def on_start(self, ctx):
        self.force_env(ctx, "external.grid_available", False)
        if self.p("water_ppm") is not None or ctx.get(TANK, "water_content_ppm", 0.0) < 500.0:
            ctx.update(TANK, water_content_ppm=float(self.p("water_ppm", 650.0)),
                       volume_l=float(self.p("fuel_l", 90.0)), fuel_quality="WATER", state="CONTAMINATED")
        self.note(f"SOC на старте: {ctx.get(INVERTER, 'battery_soc_pct')} %")

    def on_tick(self, ctx, dt):
        if ctx.get(INVERTER, "error_code") == "E04":
            self.fail(ctx, "E04 — АКБ разряжена, купол обесточен")
        self._watch_shed(ctx)
        if ctx.get(DIESEL, "fault_reason") == "WATER_IN_FUEL":
            self.note("Дизель не запустился: вода в топливе")
        if ctx.get(DIESEL, "state") == "ON":
            self.note("Дизель запущен")


class DustStorm(Crisis):
    """К2. Ветер 15 → 24 м/с, пыль: панели теряют ~2 %/мин, фильтр притока забивается в FRESH_AIR
    за ~10 мин, ветрогенератор OVERSPEED, утечка оболочки при открытых клапанах."""
    code, title, klass, kind = "К2", "Пыльная буря", "П1", "C"
    duration_s = 1500.0
    relevant_actions = ((VENT, "set_mode", "RECIRCULATION"), (SEALING, "close_valves"), (SEALING, "seal_zone"))

    def on_start(self, ctx):
        self.peak = float(self.p("peak_wind_ms", 24.0))

    def on_tick(self, ctx, dt):
        d = self.duration_s or 1500.0
        wind = _ramp(self.t, 0, 480, 15.0, self.peak) if self.t < d - 300 else _ramp(self.t, d - 300, d, self.peak, 9.0)
        wind += 1.5 * ((self.t // 40) % 3 - 1)  # порывы и затишья
        self.force_env(ctx, "outdoor.wind_speed_ms", max(0.0, wind))
        self.force_env(ctx, "outdoor.dust_level", _ramp(self.t, 0, 300, 0.5, 1.0) if self.t < d - 300 else 0.5)
        seal = ctx.node(SEALING)
        openness = sum(seal["valve_positions_pct"]) / (100.0 * len(seal["valve_positions_pct"]))
        if wind > 18.0 and openness > 0.5:
            ctx.update(SEALING, leak_rate_pct=round(seal["leak_rate_pct"] + 1.0 * dt / 60.0, 3))
        if float(ctx.get(VENT, "filter_clog_pct", 0.0)) >= 98.0:
            self.fail(ctx, "Фильтр притока забит пылью")
        if ctx.env.indoor.co2_ppm >= 1500.0:
            self.fail(ctx, "CO₂ выше 1500 ppm")


class ChainOverload(Crisis):
    """К3. Совпадение пиков: нагрев стола принтера, пуск шпинделя, паяльник (фен) Оператора.
    Перегрузка наступает не по таймеру, а по фактической сумме нагрузок (E07 → все реле на 30 с,
    повторный пик при одновременном возврате нагрузок). ⛔ Устранить №1, развести пики, после
    сбоя включать линии по очереди."""
    code, title, klass, kind = "К3", "Цепная перегрузка", "П2", "B"
    duration_s = 900.0
    relevant_actions = ((PANEL, "line_off"), (CNC, "feed_hold"), (PRINTER, "pause"))

    def on_start(self, ctx):
        self.simulate_operator = bool(self.p("simulate_operator", True))
        self.steps_done: set[str] = set()
        self.operator_changed = False
        self.prev_workstation = ctx.get(PANEL, "control_workstation")
        if self.params.get("inverter_limit_pct") is not None:
            self.set_input(ctx, INVERTER, "control_output_limit_pct", float(self.params["inverter_limit_pct"]))
        self.e07 = 0
        self._last_err = ctx.get(INVERTER, "error_code")

    def observe(self, ctx, cmd):
        if cmd.node_id == PANEL and cmd.action == "set_workstation" and cmd.origin != "crisis":
            self.operator_changed = True  # Оператор сам решил, когда паять

    def _once(self, key: str, t: float) -> bool:
        if self.t >= t and key not in self.steps_done:
            self.steps_done.add(key)
            return True
        return False

    def on_tick(self, ctx, dt):
        if self.simulate_operator:
            if self._once("printer", 0.0):
                if ctx.get(PRINTER, "state") in ("IDLE", "COMPLETED"):
                    self.operator_cmd(ctx, PRINTER, "start_job",
                                      {"file_name": "housing_lid.gcode", "duration_s": 1200, "filament_g": 45})
                else:
                    ctx.update(PRINTER, bed_temp_c=max(22.0, float(ctx.get(PRINTER, "bed_temp_c")) - 25.0))
            if self._once("cnc", 30.0):
                if ctx.get(CNC, "state") == "IDLE":
                    self.operator_cmd(ctx, CNC, "start_job", {"file_name": "pcb_power_board_2.nc", "duration_s": 1500})
                else:
                    ctx.update(CNC, spindle_rpm=0.0)  # смена инструмента: шпиндель раскручивается заново
            if self._once("solder", 60.0) and not self.operator_changed:
                ctx.command(PANEL, "set_workstation", "HOT_AIR", origin="crisis")
            if self._once("solder_end", 60.0 + float(self.p("hot_air_s", 90.0))) and not self.operator_changed:
                ctx.command(PANEL, "set_workstation", self.prev_workstation or "SOLDERING", origin="crisis")
        err = ctx.get(INVERTER, "error_code")
        if err == "E07" and self._last_err != "E07":
            self.e07 += 1
            self.fail(ctx, "Перегрузка инвертора (E07) — купол обесточен на 30 с")
            if self.e07 > 1:
                self.penalty("Повторная перегрузка при возврате питания (линии включены разом)")
        self._last_err = err


class StorageFire(Crisis):
    """К4. Замыкание зарядного устройства на складе. Настоящая тревога — пожаротушение склада
    правильно, блокировать его нельзя. Пара к №27."""
    code, title, klass, kind = "К4", "Настоящий пожар на складе", "П1", "C"
    duration_s = 1200.0
    relevant_actions = ((ACCESS, "lock_door", "STORAGE"),)

    def on_start(self, ctx):
        self.intensity = 0.0
        self.suppressed_s = 0.0
        self.suppression_started = False
        self.alarm_s: Optional[float] = None
        self.out = False

    def observe(self, ctx, cmd):
        if cmd.origin != "agent" or not cmd.ok:
            return
        blocked = (cmd.node_id == AUTO and cmd.action == "block_interlock" and "IL_SMOKE" in str(cmd.value).upper()) \
            or (cmd.node_id == FIRE and cmd.action == "block_automation" and str(cmd.value).lower() not in ("false", "0"))
        if blocked and not self.out:
            self.penalty("Заблокирована настоящая пожарная тревога")
        if cmd.node_id == FIRE and cmd.action == "stop" and not self.out:
            self.penalty("Пожаротушение склада остановлено до ликвидации очага")
        if cmd.node_id in (CNC, PRINTER) and cmd.action in ("feed_hold", "pause", "abort"):
            self.penalty("Производство в FabLab остановлено без причины")

    def on_tick(self, ctx, dt):
        fire = ctx.node(FIRE)
        sprayed = fire["state"] == "ACTIVE" and fire["active_zone"] == "STORAGE"
        if sprayed:
            self.suppression_started = True
            self.suppressed_s += dt
            self.intensity = max(0.0, self.intensity - dt / 90.0)
        elif not self.out:
            self.intensity = min(1.0, self.intensity + dt / 180.0)
        if self.intensity <= 0.0 and self.suppression_started:
            self.out = True
        fires = dict(ctx.env.drivers.fire_intensity or {})
        fires["STORAGE"] = round(self.intensity, 3)
        if ctx.get("smoke_detector_02", "alarm_state") == "ALARM" and self.alarm_s is None:
            self.alarm_s = self.t
        if self.alarm_s is not None and not self.suppression_started and self.t - self.alarm_s > 300.0:
            fires["FABLAB"] = min(0.3, (self.t - self.alarm_s - 300.0) / 600.0)  # огонь пошёл дальше
            self.fail(ctx, "Пожаротушение склада не запущено — пожар распространяется")
        ctx.env.drivers.fire_intensity = fires
        if self.out and not self.failed:
            self.resolve(ctx, "Пожар на складе потушен автоматикой")
        for m in (CNC, PRINTER):
            if ctx.get(m, "state") in ("PAUSED",) and self.timer(f"stop_{m}", True, dt) > 180.0:
                self.fail(ctx, f"{m}: производство в FabLab остановлено")
            elif ctx.get(m, "state") not in ("PAUSED",):
                self.timer(f"stop_{m}", False, dt)

    def is_finished(self, ctx):
        return self.out and ctx.env.zones["STORAGE"].smoke_density < 0.05

    def cleanup(self, ctx):
        fires = dict(ctx.env.drivers.fire_intensity or {})
        fires.pop("STORAGE", None)
        fires.pop("FABLAB", None)
        ctx.env.drivers.fire_intensity = fires

    def finalize(self, ctx):
        door = ctx.get(ACCESS, "doors", {}).get("STORAGE", {})
        self.note("Дверь склада заперта" if door.get("locked") else "Дверь склада не заперта — дым тянуло в FabLab")
        super().finalize(ctx)


class ChemicalRelease(Crisis):
    """К5. Утечка аммиака на соседнем объекте: приток затягивает NH₃ внутрь. ⛔ seal_zone + RECIRCULATION,
    но CO₂ растёт; через 20 мин развитая утечка оболочки (12 %) пропускает аммиак и при закрытых клапанах.
    Ловушка: залипший флюгер показывает ветер «от купола»."""
    code, title, klass, kind = "К5", "Химический выброс снаружи", "П1", "C"
    duration_s = 2100.0
    relevant_actions = ((SEALING, "seal_zone"), (SEALING, "close_valves"), (VENT, "set_mode", "RECIRCULATION"))

    def on_start(self, ctx):
        self.peak = float(self.p("peak_ppm", 40.0))
        self.force_env(ctx, "external.chem_substance", "NH3")
        self.force_env(ctx, "outdoor.wind_direction_deg", float(self.p("toward_dome_deg", 45.0)))
        engine = ctx.scenario_engine
        vane = engine.get("n37_stuck_wind_vane") if engine else None
        if vane is None or vane.status.value != "active":
            # флюгер «показывает» ветер от купола — выброс кажется неопасным
            self.distort(ctx, "weather_station_01", "wind_direction_deg", "const", value=225)
            self.distort(ctx, "weather_station_01", "wind_direction", "const", value="SW")
        self.leak_done = False

    def on_tick(self, ctx, dt):
        self.force_env(ctx, "external.chem_ppm", round(_ramp(self.t, 0, 300, 0.0, self.peak), 2))
        if not self.leak_done and self.t >= float(self.p("leak_at_s", 1200.0)):
            self.leak_done = True
            ctx.update(SEALING, leak_rate_pct=max(12.0, float(ctx.get(SEALING, "leak_rate_pct"))))
            self.note("Развилась утечка оболочки 12 %")
        if ctx.env.indoor.chem_ppm >= 5.0:
            self.fail(ctx, "Концентрация аммиака внутри ≥ 5 ppm")
        if ctx.env.indoor.co2_ppm >= 1500.0:
            self.fail(ctx, "CO₂ ≥ 1500 ppm при герметизации")

    def cleanup(self, ctx):
        ctx.env.unforce("external.chem_ppm")
        ctx.env.external.chem_substance = "NONE"
        ctx.env.external.chem_ppm = 0.0


class WaterFilterClog(Crisis):
    """К6. Мутность источника ×5 → картридж забивается (~4 %/мин) → перепад давления → насос работает
    без остановки и перегревается → насосная станция в FAULT → жокей-насос пожаротушения без подпитки →
    FAULT LOW_PRESSURE: купол без пожарной защиты. ⛔ switch_to_reserve / force_flush, пауза насоса."""
    code, title, klass, kind = "К6", "Засор водоочистки", "П0→П1", "C"
    duration_s = 2400.0
    relevant_actions = ((WFILTER, "switch_to_reserve"), (WFILTER, "force_flush"), (WFILTER, "replace_cartridge"),
                        ("water_pump_01", "turn_off"), ("water_pump_01", "set_power_limit"))

    def on_start(self, ctx):
        ctx.env.drivers.water_turbidity_k = float(self.p("turbidity_k", 5.0))
        self.rate = float(self.p("wear_rate_pct_min", 4.0))
        if self.params.get("start_wear_pct") is not None:
            wear = dict(ctx.get(WFILTER, "wear_pct"))
            wear[ctx.get(WFILTER, "active_filter")] = float(self.params["start_wear_pct"])
            ctx.update(WFILTER, wear_pct=wear)

    def on_tick(self, ctx, dt):
        filt = ctx.node(WFILTER)
        pump = ctx.node("water_pump_01")
        if filt["system_state"] == "NORMAL" and pump["state"] == "ON":
            wear = dict(filt["wear_pct"])
            active = filt["active_filter"]
            wear[active] = min(100.0, wear[active] + self.rate * dt / 60.0)
            ctx.update(WFILTER, wear_pct=wear)
        if pump["alarm_code"] == "OVERHEAT":
            self.note("Насос перегрелся (FAULT OVERHEAT)")
        if ctx.get(FIRE, "state") == "FAULT":
            self.fail(ctx, "Пожаротушение в FAULT LOW_PRESSURE — купол без пожарной защиты")

    def cleanup(self, ctx):
        ctx.env.drivers.water_turbidity_k = 1.0


class CoolingFailure(Crisis):
    """К7. Воздух в контуре охлаждения: расход падает, жидкость и шпиндель греются
    (> 40 °C — хуже качество, > 45 °C — перегрев). ⛔ насос 100 %, пауза фрезера, прокачка контура."""
    code, title, klass, kind = "К7", "Отказ охлаждения станков", "П2", "B"
    duration_s = 1800.0
    relevant_actions = ((COOLING, "set_speed"), (CNC, "feed_hold"))

    def on_start(self, ctx):
        ctx.update(COOLING, control_air_in_loop=True)

    def on_tick(self, ctx, dt):
        cnc = ctx.node(CNC)
        if cnc["state"] == "RUNNING" and cnc["spindle_temp_c"] > 45.0:
            self.fail(ctx, "Фрезер работал при перегреве шпинделя (> 45 °C)")
        if not ctx.get(COOLING, "control_air_in_loop") and float(ctx.get(COOLING, "coolant_temp_c")) < 40.0:
            self.resolve(ctx, "Контур прокачан, жидкость остыла")

    def is_finished(self, ctx):
        return self.resolved and self.t > 60.0


class EdgeMemoryLeak(Crisis):
    """К8. Утечка памяти в telemetry_collector: −3 %/мин. < 15 % — телеметрия части узлов замирает,
    < 5 % — падает agent_runtime. ⛔ restart_service(telemetry_collector); reboot — крайняя мера."""
    code, title, klass, kind = "К8", "Утечка памяти edge-узла", "П1", "B/D"
    duration_s = 1800.0
    relevant_actions = ((EDGE, "restart_service", "telemetry_collector"),)

    def on_start(self, ctx):
        ctx.update(EDGE, control_memory_leak_pct_min=float(self.p("leak_pct_min", 3.0)))

    def observe(self, ctx, cmd):
        if cmd.origin == "agent" and cmd.node_id == EDGE and cmd.action == "reboot" and cmd.ok:
            self.penalty("Полная перезагрузка edge (3 минуты без телеметрии)")
            self.fail(ctx, "Выполнен reboot вместо перезапуска сервиса")

    def on_tick(self, ctx, dt):
        services = ctx.get(EDGE, "services", {})
        if services.get("agent_runtime") == "FAILED":
            self.fail(ctx, "Упал agent_runtime (память < 5 %)")
        if float(ctx.get(EDGE, "control_memory_leak_pct_min") or 0.0) == 0.0:
            self.resolve(ctx, "Сборщик телеметрии перезапущен, утечка устранена")

    def is_finished(self, ctx):
        return self.resolved or self.failed

    def cleanup(self, ctx):
        ctx.update(EDGE, control_memory_leak_pct_min=0.0)


class SolarFlare(Crisis):
    """К9. Солнечная вспышка: фон 0.12 → 0.4 мкЗв/ч, шум эфира +20 дБ (FSK теряет приём),
    спутниковый резерв недоступен. ⛔ радио на LORA, резерв на MESH; герметизация не нужна."""
    code, title, klass, kind = "К9", "Солнечная вспышка", "П0", "C"
    duration_s = 1200.0
    relevant_actions = (("radio_01", "set_protocol"), ("backup_comms_01", "switch_channel"))

    def on_start(self, ctx):
        self.force_env(ctx, "external.radio_noise_db", float(self.p("noise_db", 20.0)))
        self.force_env(ctx, "external.solar_flare", True)

    def observe(self, ctx, cmd):
        if cmd.origin == "agent" and cmd.ok and (
                (cmd.node_id == SEALING and cmd.action in ("seal_zone", "close_valves"))
                or (cmd.node_id == ACCESS and cmd.action == "lockdown")):
            self.penalty("Лишняя герметизация/блокировка из-за радиационного WARNING")

    def on_tick(self, ctx, dt):
        self.force_env(ctx, "external.radiation_usv_h", round(_ramp(self.t, 0, 120, 0.12, 0.4), 3))
        ok = ctx.get("radio_01", "reception") and ctx.get("backup_comms_01", "available")
        if ok and self.t > 8.0:
            if self.t <= float(self.p("deadline_s", 600.0)):
                self.resolve(ctx, "Связь восстановлена (радио и резервный канал)")
        elif self.t > float(self.p("deadline_s", 600.0)) and not self.resolved:
            self.fail(ctx, "Связь не восстановлена за 10 минут")

    def cleanup(self, ctx):
        ctx.env.unforce("external.radiation_usv_h")


class WeakEarthquake(Crisis):
    """К10. M3.8 в 80 км: сейсмодатчик, утечка оболочки +10 %, дверь склада FORCED, УЗО резервной линии,
    возможен афтершок через 10 мин. ⛔ Не останавливать производство, классифицировать тревоги."""
    code, title, klass, kind = "К10", "Слабое землетрясение", "П1", "C"
    duration_s = 900.0

    def on_start(self, ctx):
        ctx.env.inject_quake(float(self.p("magnitude", 3.8)), float(self.p("distance_km", 80.0)), 20.0)
        ctx.update(SEALING, leak_rate_pct=round(float(ctx.get(SEALING, "leak_rate_pct")) + 10.0, 3))
        doors = {k: dict(v) for k, v in ctx.get(ACCESS, "doors").items()}
        doors["STORAGE"]["state"] = "FORCED"
        ctx.update(ACCESS, doors=doors, alarm=True, alarm_reason="STORAGE_FORCED", system_state="ALARM")
        self.cmd(ctx, PANEL, "emulate_protection_trip", {"line": int(self.p("line", 7)), "type": "RCD_TRIP"})
        self.aftershock = False

    def observe(self, ctx, cmd):
        if cmd.origin != "agent" or not cmd.ok:
            return
        if cmd.node_id in (CNC, PRINTER) and cmd.action in ("feed_hold", "pause", "abort"):
            self.fail(ctx, "Производство остановлено без причины (толчок станки не затронул)")
        n = self.agent_turned_off(cmd, PRODUCTION_LINES)
        if n is not None:
            self.fail(ctx, f"Отключена производственная линия {n} без причины")

    def on_tick(self, ctx, dt):
        if not self.aftershock and self.t >= float(self.p("aftershock_s", 600.0)):
            self.aftershock = True
            ctx.env.inject_quake(3.0, float(self.p("distance_km", 80.0)), 12.0)

    def finalize(self, ctx):
        super().finalize(ctx)
        self.note("Классификация тревог и сообщение Оператору оцениваются по журналу")


class Frost(Crisis):
    """К11. Снаружи −15 °C: LiFePO4 нельзя заряжать ниже 0 °C (BMS запрещает заряд при солнце),
    топливо загустевает (дизель START_FAILURE без подогрева), анемометр обмерзает (№18)."""
    code, title, klass, kind = "К11", "Мороз", "П1", "C"
    duration_s = 2400.0
    relevant_actions = ((VENT, "set_mode", "RECIRCULATION"), (DIESEL, "preheat"))

    def on_start(self, ctx):
        self.start_t = ctx.env.outdoor.temperature_c
        self.target = float(self.p("temp_c", -15.0))
        ctx.update(TANK, fuel_temp_c=float(self.p("fuel_temp_c", -6.0)))
        ctx.update(BATTERY, temperature_c=float(self.p("battery_temp_c", 1.0)))
        self.icing = False

    def on_tick(self, ctx, dt):
        self.force_env(ctx, "outdoor.temperature_c", round(_ramp(self.t, 0, 60, self.start_t, self.target), 2))
        if not self.icing and self.t >= float(self.p("icing_at_s", 600.0)) and ctx.scenario_engine is not None:
            self.icing = True
            ctx.scenario_engine.trigger_crisis("n18_wind_overestimate", ctx, {"duration_s": 900})
        if ctx.get(INVERTER, "error_code") == "E04":
            self.fail(ctx, "Купол обесточен (E04)")
        if not ctx.get(BATTERY, "charge_allowed"):
            self.note("BMS запретила заряд АКБ (температура ниже 0 °C)")

    def finalize(self, ctx):
        super().finalize(ctx)
        self.note("Сообщение верного диагноза («АКБ не заряжается от холода») оценивается по журналу")


class DoubleLie(Crisis):
    """К12. Основной датчик CO₂ ложно высокий + резервный climate_sensor_03 ускоренно дрейфует вверх:
    «2 из 3» подтверждают тревогу, истинный CO₂ в норме."""
    code, title, klass, kind = "К12", "Двойная ложь", "П3", "A"
    duration_s = 1200.0
    relevant_actions = ((AUTO, "set_data_trust", {"node": "climate_sensor_01"}),
                        (AUTO, "set_data_trust", {"node": "climate_sensor_03"}))

    def on_start(self, ctx):
        self.distort(ctx, "climate_sensor_01", "co2_ppm", "const", value=float(self.p("ppm", 3000.0)),
                     noise=30.0, round=0)
        self.set_input(ctx, "climate_sensor_03", "control_co2_drift_ppm_min", float(self.p("drift_ppm_min", 150.0)))

    def on_tick(self, ctx, dt):
        for m in (PRINTER, CNC):
            if self.timer(f"stop_{m}", ctx.get(m, "state") == "PAUSED", dt) > 120.0:
                self.fail(ctx, f"{m} остановлен по ложной тревоге CO₂ дольше 2 минут")
        if self.agent_marked(ctx, "climate_sensor_01", "co2_ppm", "*") \
                and self.agent_marked(ctx, "climate_sensor_03", "co2_ppm", "*") and not self.failed:
            self.resolve(ctx, "Оба неисправных датчика помечены")

    success_if_not_failed = False


class FilamentShortage(Crisis):
    """К13. На принтере катушка, которой не хватит на корпус: обрыв филамента на ~70 %.
    ⛔ До начала печати (или в первые минуты) — попросить Оператора сменить катушку."""
    code, title, klass, kind = "К13", "Филамента не хватит", "П2", "B"
    success_if_not_failed = False
    duration_s = None

    def on_start(self, ctx):
        ctx.instance(INVENTORY).set_spool(float(self.p("spool_g", 180.0)))
        self.replaced = False

    def observe(self, ctx, cmd):
        if cmd.node_id == INVENTORY and cmd.action == "replace_spool" and cmd.ok:
            self.replaced = True
            self.resolve(ctx, "Катушка заменена до обрыва")

    def on_tick(self, ctx, dt):
        if ctx.get(PRINTER, "error_code") == "FILAMENT_RUNOUT" and not self.replaced:
            self.fail(ctx, "Обрыв филамента во время печати корпуса")

    def is_finished(self, ctx):
        return self.replaced or self.failed or self.t > float(self.p("max_s", 5400.0))


CASCADE_CRISES: dict[str, type[Crisis]] = {
    "k01_night_without_reserve": NightWithoutReserve,
    "k02_dust_storm": DustStorm,
    "k03_chain_overload": ChainOverload,
    "k04_storage_fire": StorageFire,
    "k05_chemical_release": ChemicalRelease,
    "k06_water_filter_clog": WaterFilterClog,
    "k07_cooling_failure": CoolingFailure,
    "k08_edge_memory_leak": EdgeMemoryLeak,
    "k09_solar_flare": SolarFlare,
    "k10_weak_earthquake": WeakEarthquake,
    "k11_frost": Frost,
    "k12_double_lie": DoubleLie,
    "k13_filament_shortage": FilamentShortage,
}

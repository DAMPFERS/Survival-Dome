# dome_simulator/rules.py
"""
Правила зависимостей между узлами песочницы.

Каждое правило — функция (ctx: SimContext, dt: float). Выполняются каждый тик
после узлов и кризисов, в порядке build_default_rules(); результат попадает
во входы control_* узлов и в драйверы воздуха окружения (env.drivers) и
учитывается узлами на следующем тике.

Цепочки:
    энергия      панели + ветер → инвертор ← дизель (AC-вход) ← бак;
                 инвертор ↔ АКБ (BMS: зеркало SOC, запрет заряда на морозе);
                 выход инвертора → шина щита → линии → ЧПУ, принтер, вытяжка;
                 edge и приток — от выхода инвертора; нагрузка щита и служб → инвертор;
                 блэкаут и сброс нагрузки на реальном щите — командами реле (Д19)
    производство принтер → катушка на складе (расход, обрыв филамента);
                 ЧПУ/принтер → тепловая нагрузка контура охлаждения → температура шпинделя
    воздух       приток, клапаны и утечки оболочки → воздухообмен купола;
                 пайка (линия 1), печать, вытяжка → VOC; дверь склада → дым/CO из склада;
                 приток → температура техпомещения АКБ
    вода         резервуар (поплавок) → насос → фильтр → резервуар;
                 перепад на фильтре → нагрузка насоса; пожаротушение ← вода резервуара;
                 пожаротушение в FABLAB → вода на линии 1 (УЗО)
    автоматика   интерлоки ПЛК по телеметрии (nodes/automation.py), вентиляция по CO₂,
                 автозапуск дизеля
"""
from __future__ import annotations

import math
from typing import Any, Callable, Optional

from .context import SimContext
from .environment import clamp
from .events import Severity
from .nodes.automation import INTERLOCKS, trust_key
from .nodes.power import PANEL_LINES
from .nodes.water import water_demand_l_min

RuleFunc = Callable[[SimContext, float], None]

INVERTER, BATTERY, PANELS, WIND, DIESEL, TANK = ("solar_inverter_01", "battery_01", "solar_panels_01",
                                                 "wind_turbine_01", "dizel_1", "fuel_tank_01")
PANEL, PRINTER, CNC, FUME = "smart_panel_01", "printer_3d_01", "cnc_01", "fume_extraction_01"
VENT, SEALING, EDGE, COOLING, INVENTORY = ("supply_ventilation_01", "dome_sealing_01", "edge_compute_01",
                                           "equipment_cooling_01", "material_inventory_01")
PUMP, WTANK, WFILTER, FIRE, ACCESS = ("water_pump_01", "water_tank_01", "water_filter_01",
                                      "fire_suppression_01", "access_control_01")
AUTO, WEATHER = "dome_automation_01", "weather_station_01"
SMOKE_DETECTORS = ("smoke_detector_01", "smoke_detector_02")

SOLDER_THRESHOLD_W = 200.0       # нагрузка линии 1 выше — идёт пайка
EDGE_POWER_W = 60.0
BUS_ALIVE_V = 100.0


def _present(ctx: SimContext, *node_ids: str) -> bool:
    return all(ctx.has(n) for n in node_ids)


def _set_if_changed(ctx: SimContext, node_id: str, **values: Any) -> None:
    """Пишет только изменившиеся входы (меньше копирований в хранилище)."""
    if not ctx.has(node_id) or ctx.is_real(node_id):
        return
    node = ctx.node(node_id)
    changed = {k: v for k, v in values.items() if node.get(k) != v}
    if changed:
        ctx.update(node_id, **changed)


# ---------------------------------------------------------------------------
# Энергия: источники
# ---------------------------------------------------------------------------

def power_sources_rule(ctx: SimContext, dt: float) -> None:
    if dt <= 0 or not ctx.has(INVERTER):
        return
    inv = ctx.node(INVERTER)

    pv = None
    if ctx.energy_mode == "demo" and ctx.real_pv_power_w is not None:
        pv = float(ctx.real_pv_power_w)       # демо-режим: реальная генерация солнечного инвертора
    elif ctx.has(PANELS):
        pv = float(ctx.get(PANELS, "power_w", 0.0))
    wind = float(ctx.get(WIND, "power_w", 0.0)) if ctx.has(WIND) else 0.0

    generator_w = 0.0
    if _present(ctx, DIESEL):
        diesel_node = ctx.node(DIESEL)
        generator_w = ctx.instance(DIESEL).available_power_w(diesel_node)
        _set_if_changed(ctx, DIESEL, control_load_w=float(inv.get("generator_power_w") or 0.0))
        if ctx.has(TANK):
            tank = ctx.node(TANK)
            _set_if_changed(ctx, DIESEL,
                            control_fuel_available=tank["volume_l"] > 0.5,
                            control_fuel_quality=tank["fuel_quality"],
                            control_fuel_temp_c=tank["fuel_temp_c"])
            _set_if_changed(ctx, TANK, control_consumption_l_h=float(diesel_node.get("fuel_consumption_l_h", 0.0)))

    charge_allowed = None
    if ctx.has(BATTERY):
        batt = ctx.node(BATTERY)
        charge_allowed = bool(batt.get("charge_allowed", True))
        _set_if_changed(ctx, BATTERY,
                        control_soc_pct=inv["battery_soc_pct"],
                        control_power_w=inv["battery_power_w"])
    _set_if_changed(ctx, INVERTER, control_pv_power_w=pv, control_wind_power_w=wind,
                    control_generator_w=generator_w, control_charge_allowed=charge_allowed)


# ---------------------------------------------------------------------------
# Энергия: распределение по щиту и нагрузкам
# ---------------------------------------------------------------------------

def _device_demand_w(ctx: SimContext, node_id: str) -> float:
    """Сколько устройство потребит от линии, если на неё подано питание."""
    node = ctx.node(node_id)
    if node.get("control_powered", True):
        return float(node.get("power_w") or 0.0)
    if node_id == FUME:
        return 95.0 if node.get("enabled") and node.get("motor_state") == "OK" else 0.0
    return 25.0  # контроллер/электроника стартует


def power_distribution_rule(ctx: SimContext, dt: float) -> None:
    if dt <= 0 or not ctx.has(PANEL):
        return
    panel_real = ctx.is_real(PANEL)
    if ctx.has(INVERTER):
        inv = ctx.node(INVERTER)
        bus_powered = ctx.is_real(INVERTER) or float(inv["output_voltage_v"]) > BUS_ALIVE_V
        bus_v = float(inv["output_voltage_v"]) if not ctx.is_real(INVERTER) else 230.0
    else:
        bus_powered, bus_v = True, 230.0

    # Реальные реле: блэкаут виртуальной энергосистемы = выключение реле (Д19)
    mem = ctx.memory.setdefault("blackout", {"lines": [], "active": False})
    if panel_real:
        lines = ctx.store.get(PANEL, "lines")
        if not bus_powered and not mem["active"]:
            mem["active"] = True
            mem["lines"] = [l["line"] for l in lines if l["state"] == "ON"]
            for n in mem["lines"]:
                ctx.real_commands.append((PANEL, "line_off", n))
            ctx.emit("power.blackout_relays_off", Severity.CRITICAL, source="rules", lines=mem["lines"])
        elif bus_powered and mem["active"]:
            mem["active"] = False
            for n in mem["lines"]:
                ctx.real_commands.append((PANEL, "line_on", n))
            mem["lines"] = []
    else:
        _set_if_changed(ctx, PANEL, control_bus_powered=bus_powered, control_bus_voltage_v=round(bus_v, 1))

    panel = ctx.node(PANEL)
    powered = panel.get("bus_powered", True) if not panel_real else bus_powered

    def live(n: int) -> bool:
        return panel["lines"][n - 1]["state"] == "ON" and powered

    for node_id, line in ((CNC, 2), (PRINTER, 3), (FUME, 6)):
        _set_if_changed(ctx, node_id, control_powered=live(line))
    for node_id in (EDGE, VENT):
        _set_if_changed(ctx, node_id, control_powered=bool(bus_powered))

    loads = {}
    for node_id, line in ((CNC, 2), (PRINTER, 3), (FUME, 6)):
        if ctx.has(node_id):
            loads[str(line)] = round(_device_demand_w(ctx, node_id), 1)
    _set_if_changed(ctx, PANEL, control_line_loads_w=loads)

    # Нагрузка инвертора = щит + службы купола (приток, охлаждение, насос, edge, связь)
    services = 15.0
    if ctx.has(VENT):
        services += float(ctx.get(VENT, "power_w", 0.0))
    if ctx.has(COOLING):
        services += float(ctx.get(COOLING, "pump_power_w", 0.0))
    if ctx.has(EDGE) and ctx.get(EDGE, "state") != "OFFLINE":
        services += EDGE_POWER_W
    if ctx.has(PUMP):
        pump = ctx.node(PUMP)
        services += float(pump["power_limit_pct"]) / 100.0 * 750.0 * float(pump["duty_cycle_pct"]) / 100.0
    panel_total = float(panel.get("total_power_w", 0.0))
    if panel_real and powered:
        # реальный щит не видит виртуальную утечку (№1) — добавляем её в энергобаланс
        panel_total += float(panel.get("control_phantom_load_w") or 0.0)
    if ctx.has(INVERTER):
        _set_if_changed(ctx, INVERTER, control_load_w=round(panel_total + services, 1))


# ---------------------------------------------------------------------------
# Производство
# ---------------------------------------------------------------------------

def production_rule(ctx: SimContext, dt: float) -> None:
    if dt <= 0:
        return
    if _present(ctx, PRINTER, INVENTORY):
        printer = ctx.instance(PRINTER)
        inventory = ctx.instance(INVENTORY)
        _set_if_changed(ctx, INVENTORY, control_filament_usage_g_h=round(printer.filament_rate_g_h(ctx.node(PRINTER)), 3))
        _set_if_changed(ctx, PRINTER, control_spool_g=round(inventory.true_filament_g, 2))
    if ctx.has(COOLING):
        heat = 0.0
        if ctx.has(CNC):
            cnc = ctx.node(CNC)
            heat += 340.0 * float(cnc.get("spindle_rpm", 0.0)) / 10000.0
        if ctx.has(PRINTER) and ctx.get(PRINTER, "state") in ("PRINTING", "PAUSED"):
            heat += 60.0
        heat += 15.0  # электроника драйверов
        _set_if_changed(ctx, COOLING, control_heat_load_w=round(heat, 1))
        if ctx.has(CNC):
            _set_if_changed(ctx, CNC, control_coolant_temp_c=ctx.get(COOLING, "coolant_temp_c"))


# ---------------------------------------------------------------------------
# Воздух купола
# ---------------------------------------------------------------------------

def air_rule(ctx: SimContext, dt: float) -> None:
    if dt <= 0:
        return
    d = ctx.env.drivers
    if ctx.has(VENT):
        vent = ctx.node(VENT)
        d.fresh_air_m3_h = float(vent.get("fresh_air_m3_h", 0.0))
        if ctx.has(BATTERY):
            airflow = float(vent.get("airflow_m3_h", 0.0))
            # техпомещение АКБ на приточном воздуховоде: доля наружного воздуха в притоке
            share = clamp(d.fresh_air_m3_h / airflow, 0.0, 1.0) * min(1.0, airflow / 300.0) if airflow > 1 else 0.0
            _set_if_changed(ctx, BATTERY, control_fresh_air_share=round(share, 3))
        if ctx.has(SEALING):
            _set_if_changed(ctx, SEALING, control_supply_airflow_m3_h=float(vent.get("airflow_m3_h", 0.0)))
    if ctx.has(SEALING):
        seal = ctx.node(SEALING)
        openness = sum(seal["valve_positions_pct"]) / (100.0 * max(1, len(seal["valve_positions_pct"])))
        wind = ctx.env.outdoor.wind_speed_ms
        d.infiltration_m3_h = round(float(seal["leak_rate_pct"]) * 0.8 + openness * (20.0 + 1.5 * wind), 2)
    if ctx.has(FUME):
        d.fume_extraction_on = float(ctx.get(FUME, "fan_rpm", 0.0)) > 500
    if ctx.has(PRINTER):
        d.printing = ctx.get(PRINTER, "state") == "PRINTING"
    if ctx.has(PANEL):
        line1 = ctx.line(1)
        d.solder_active = float(line1.get("power_w", 0.0)) > SOLDER_THRESHOLD_W
    if ctx.has(ACCESS):
        door = ctx.get(ACCESS, "doors", {}).get("STORAGE", {})
        d.storage_door_open = (not door.get("locked", True)) or door.get("state") in ("OPEN", "FORCED")
    if ctx.has(FIRE):
        fire = ctx.node(FIRE)
        d.suppression_zones = (fire["active_zone"],) if fire["state"] == "ACTIVE" and fire["active_zone"] else ()


# ---------------------------------------------------------------------------
# Вода и пожаротушение
# ---------------------------------------------------------------------------

def water_rule(ctx: SimContext, dt: float) -> None:
    if dt <= 0 or not _present(ctx, PUMP, WTANK, WFILTER):
        return
    tank = ctx.node(WTANK)
    filt = ctx.node(WFILTER)
    pump = ctx.node(PUMP)
    level = float(tank["volume_l"]) / float(tank["capacity_l"])
    float_valve_l_min = clamp(0.3 + 3.0 * (0.7 - level), 0.0, 40.0)
    _set_if_changed(ctx, PUMP, control_demand_l_min=round(float_valve_l_min, 3),
                    control_filter_dp_bar=round(float(filt["pressure_drop_bar"]), 3))
    flushing = filt["system_state"] == "FLUSHING"
    delivered = 0.0 if flushing else float(pump.get("delivered_l_min", 0.0))
    _set_if_changed(ctx, WFILTER, control_flow_l_min=round(delivered, 3))
    discharge = float(ctx.get(FIRE, "discharge_l_min", 0.0)) if ctx.has(FIRE) else 0.0
    dome_use = water_demand_l_min(ctx.env.outdoor.hour, ctx.env.indoor.occupancy)
    _set_if_changed(ctx, WTANK, control_inflow_l_min=round(delivered, 3),
                    control_outflow_l_min=round(dome_use + discharge, 3))
    if ctx.has(FIRE):
        _set_if_changed(ctx, FIRE, control_water_available_l=round(float(tank["volume_l"]), 1),
                        control_supply_ok=pump["state"] != "FAULT")

    # Пожаротушение в FABLAB заливает рабочее место: через 2 мин УЗО линии 1
    if ctx.has(FIRE) and ctx.has(PANEL):
        fire = ctx.node(FIRE)
        mem = ctx.memory.setdefault("flooding", {"s": 0.0, "tripped": False})
        if fire["state"] == "ACTIVE" and fire["active_zone"] == "FABLAB":
            mem["s"] += dt
            if mem["s"] >= 120.0 and not mem["tripped"]:
                mem["tripped"] = True
                if ctx.line(1)["state"] == "ON":
                    ctx.command(PANEL, "emulate_protection_trip", {"line": 1, "type": "RCD_TRIP"}, origin="system")
                ctx.emit("fablab.flooded", Severity.CRITICAL, source="rules", zone="FABLAB")
        else:
            mem["s"] = 0.0
            mem["tripped"] = False


# ---------------------------------------------------------------------------
# Автоматика купола (интерлоки)
# ---------------------------------------------------------------------------

class _Reader:
    """Чтение телеметрии для ПЛК с учётом доверия к источникам."""

    def __init__(self, ctx: SimContext, trust: dict[str, str]) -> None:
        self.ctx = ctx
        self.trust = trust
        self._views: dict[str, dict[str, Any]] = {}

    def trusted(self, node_id: str, path: str) -> bool:
        return self.trust.get(trust_key(node_id, path), self.trust.get(f"{node_id}.*", "TRUSTED")) == "TRUSTED"

    def value(self, node_id: str, path: str) -> Any:
        if not self.ctx.has(node_id) or not self.trusted(node_id, path):
            return None
        if node_id not in self._views:
            self._views[node_id] = self.ctx.view(node_id)
        from .telemetry import get_path
        return get_path(self._views[node_id], path)

    def number(self, node_id: str, path: str) -> Optional[float]:
        v = self.value(node_id, path)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            return float(v)
        return None


def _log(events: list[dict[str, Any]], ctx: SimContext, auto: Any, interlock: str, kind: str,
         message: str, **extra: Any) -> dict[str, Any]:
    entry = {"event_id": auto.next_event_id(), "t": round(ctx.clock(), 1), "t_s": round(ctx.now_s, 1),
             "interlock": interlock, "type": kind, "message": message, "acknowledged": False, **extra}
    events.append(entry)
    return entry


def _line_off(ctx: SimContext, n: int, shed: list[int]) -> None:
    line = ctx.line(n)
    if line["state"] == "ON" and ctx.command(PANEL, "line_off", n, origin="automation"):
        shed.append(n)


def _pause_machines(ctx: SimContext, paused: list[str]) -> None:
    if ctx.has(PRINTER) and ctx.get(PRINTER, "state") == "PRINTING":
        if ctx.command(PRINTER, "pause", origin="automation"):
            paused.append(PRINTER)
    if ctx.has(CNC) and ctx.get(CNC, "state") == "RUNNING":
        if ctx.command(CNC, "feed_hold", origin="automation"):
            paused.append(CNC)


def _backup_available(r: _Reader, ctx: SimContext) -> bool:
    """Есть ли внешний резерв: сеть или готовый к запуску/работающий дизель (по телеметрии)."""
    if r.value(INVERTER, "grid_state") == "AVAILABLE":
        return True
    state = r.value(DIESEL, "state")
    if state == "ON":
        return True
    if state == "OFF" and r.value(DIESEL, "fault_code") is None:
        quality = r.value(TANK, "fuel_quality")
        level = r.number(TANK, "level_pct")
        return quality == "GOOD" and (level is None or level > 10.0)
    return False


def automation_rule(ctx: SimContext, dt: float) -> None:
    if dt <= 0 or not ctx.has(AUTO):
        return
    auto = ctx.instance(AUTO)
    a = ctx.node(AUTO)
    if a.get("mode") != "AUTO":
        return
    ils = {k: dict(v) for k, v in a["interlocks"].items()}
    events = [dict(e) for e in a["events"]]
    r = _Reader(ctx, a["data_trust"])
    mem = ctx.memory.setdefault("automation", {})
    now = ctx.now_s

    # --- запросы агента «снять интерлок» ---
    for req in a.get("control_requests", []):
        if req.get("op") == "release":
            _release(ctx, req["id"], ils[req["id"]], events, auto)

    primary = a.get("primary_co2_sensor") or "climate_sensor_01"

    def step(il: str, condition: bool, delay: float) -> bool:
        """Общая логика задержки. Возвращает True в момент срабатывания."""
        st = ils[il]
        if st["state"] == "BLOCKED":
            return False
        if condition:
            st["pending_s"] = st.get("pending_s", 0.0) + dt
            if st["state"] == "ARMED" and st["pending_s"] >= delay:
                st["state"] = "TRIPPED"
                st["trips"] = st.get("trips", 0) + 1
                st["tripped_at_s"] = round(now, 1)
                return True
            if st["state"] == "ARMED":
                st["state_detail"] = "PENDING"
        else:
            st["pending_s"] = 0.0
            st.pop("state_detail", None)
            if st["state"] == "TRIPPED":
                st["state"] = "ARMED"
                st.pop("suppression_countdown_s", None)
                _log(events, ctx, auto, il, "CLEAR", "Условие снято, интерлок перевзведён")
        return False

    def trip_event(il: str, message: str, **extra: Any) -> dict[str, Any]:
        ctx.emit("automation.interlock_tripped", Severity.CRITICAL, source=AUTO, interlock=il, message=message)
        return _log(events, ctx, auto, il, "TRIP", message, **extra)

    # --- IL_CO2 ---
    co2 = r.number(primary, "co2_ppm")
    if step("IL_CO2", co2 is not None and co2 > 2000.0, INTERLOCKS["IL_CO2"]["delay_s"]):
        paused: list[str] = []
        _pause_machines(ctx, paused)
        st = ils["IL_CO2"]
        if ctx.has(VENT):
            vent = ctx.node(VENT)
            st["prev_vent"] = {"mode": vent["mode"], "speed_pct": vent["speed_pct"]}
            ctx.command(VENT, "set_mode", "FRESH_AIR", origin="automation")
            ctx.command(VENT, "set_speed", 100, origin="automation")
        st["paused"] = paused
        trip_event("IL_CO2", f"CO₂ {co2:.0f} ppm ({primary}): станки на паузе, приток 100 %", paused=paused)

    # --- IL_CONDENSATE ---
    rh = r.number(primary, "humidity_pct")
    t = r.number(primary, "temperature_c")
    dew = r.number(primary, "dew_point_c")
    if not r.trusted(primary, "humidity_pct"):
        dew = None  # точка росы вычислена из недоверенного канала влажности
    condensate = (rh is not None and rh > 90.0) or (t is not None and dew is not None and t - dew < 1.0)
    if step("IL_CONDENSATE", condensate, INTERLOCKS["IL_CONDENSATE"]["delay_s"]):
        paused = []
        if ctx.has(CNC) and ctx.get(CNC, "state") == "RUNNING" and ctx.command(CNC, "feed_hold", origin="automation"):
            paused.append(CNC)
        ils["IL_CONDENSATE"]["paused"] = paused
        trip_event("IL_CONDENSATE", f"Риск конденсата (RH {rh}): ЧПУ на паузе", paused=paused)

    # --- IL_SMOKE (по зонам: у каждой зоны свой отсчёт до пожаротушения) ---
    alarm_zones: list[str] = []
    for det in SMOKE_DETECTORS:
        if ctx.has(det) and r.value(det, "alarm_state") == "ALARM":
            zone = ctx.get(det, "zone", "FABLAB")
            if zone not in alarm_zones:
                alarm_zones.append(zone)
    st = ils["IL_SMOKE"]
    zones = {z: dict(v) for z, v in (st.get("zones") or {}).items()}
    if st["state"] != "BLOCKED":
        for zone in alarm_zones:
            if zone in zones:
                continue
            paused, shed = [], []
            if zone == "FABLAB":
                _pause_machines(ctx, paused)
                _line_off(ctx, 1, shed)
            entry = trip_event("IL_SMOKE", f"Дым в зоне {zone}: через 60 с без квитирования — пожаротушение",
                               zone=zone)
            zones[zone] = {"event_id": entry["event_id"], "paused": paused,
                           "countdown_s": INTERLOCKS["IL_SMOKE"]["suppression_delay_s"]}
            st["shed_lines"] = sorted(set(st.get("shed_lines", [])) | set(shed))
            st["trips"] = st.get("trips", 0) + 1
            st["tripped_at_s"] = round(now, 1)
        for zone in [z for z in zones if z not in alarm_zones]:
            del zones[zone]
            _log(events, ctx, auto, "IL_SMOKE", "CLEAR", f"Тревога зоны {zone} снята")
        for zone, zst in zones.items():
            if zst.get("countdown_s") is None:
                continue
            if any(e["event_id"] == zst["event_id"] and e["acknowledged"] for e in events):
                zst["countdown_s"] = None
                _log(events, ctx, auto, "IL_SMOKE", "ACTION",
                     f"Тревога зоны {zone} квитирована — автопуск пожаротушения отменён")
                continue
            zst["countdown_s"] -= dt
            if zst["countdown_s"] <= 0:
                zst["countdown_s"] = None
                zst["requested"] = True
                _log(events, ctx, auto, "IL_SMOKE", "ACTION", f"Пуск пожаротушения зоны {zone}")
        # запрос держится, пока пожаротушение не пойдёт в этой зоне (система может быть занята)
        request = None
        if ctx.has(FIRE):
            fire = ctx.node(FIRE)
            for zone, zst in zones.items():
                if zst.get("requested"):
                    if fire["state"] == "ACTIVE" and fire["active_zone"] == zone:
                        zst["requested"] = False
                        zst["suppressed"] = True
                    elif request is None:
                        request = zone
            _set_if_changed(ctx, FIRE, control_auto_trigger_zone=request)
        st["zones"] = zones
        st["state"] = "TRIPPED" if zones else "ARMED"
        if not zones:
            st.pop("shed_lines", None)
    elif ctx.has(FIRE):
        _set_if_changed(ctx, FIRE, control_auto_trigger_zone=None)  # автоматика заблокирована агентом
    # --- IL_LOAD_SHED ---
    soc = r.number(BATTERY, "soc_pct")
    if soc is None:
        soc = r.number(INVERTER, "battery_soc_pct")
    cap = r.number(BATTERY, "estimated_capacity_kwh")
    load = r.number(INVERTER, "load_power_w")
    lux = r.number(WEATHER, "illuminance_lux")
    pv_forecast = (lux / 110.0) * 1.2 * 0.85 if lux is not None else (r.number(INVERTER, "pv_power_w") or 0.0)
    autonomy = None
    backup = _backup_available(r, ctx)
    if soc is not None and cap is not None and load is not None and not backup:
        net = load - pv_forecast
        autonomy = math.inf if net <= 20.0 else max(0.0, (soc - 20.0) / 100.0 * cap * 1000.0 / net)
    deficit = r.number(INVERTER, "power_deficit_w")
    spec = INTERLOCKS["IL_LOAD_SHED"]
    mem.setdefault("shed_soc_s", 0.0)
    mem.setdefault("shed_aut_s", 0.0)
    mem.setdefault("shed_def_s", 0.0)
    mem["shed_soc_s"] = mem["shed_soc_s"] + dt if soc is not None and soc < 25.0 else 0.0
    mem["shed_aut_s"] = mem["shed_aut_s"] + dt if autonomy is not None and autonomy < 1.0 else 0.0
    mem["shed_def_s"] = mem["shed_def_s"] + dt if deficit is not None and deficit > 20.0 else 0.0
    cond_active = mem["shed_soc_s"] > 0 or mem["shed_aut_s"] > 0 or mem["shed_def_s"] > 0
    ready = (mem["shed_soc_s"] >= spec["soc_delay_s"] or mem["shed_aut_s"] >= spec["autonomy_delay_s"]
             or mem["shed_def_s"] >= spec["deficit_delay_s"])
    st = ils["IL_LOAD_SHED"]
    if st["state"] != "BLOCKED":
        if ready and st["state"] == "ARMED":
            st.update(state="TRIPPED", trips=st.get("trips", 0) + 1, tripped_at_s=round(now, 1), stage=1)
            shed: list[int] = []
            for n in (8, 7, 1):
                _line_off(ctx, n, shed)
            st["shed_lines"] = shed
            reason = ("SOC" if mem["shed_soc_s"] >= spec["soc_delay_s"] else
                      "дефицит мощности" if mem["shed_def_s"] >= spec["deficit_delay_s"] else "прогноз автономности")
            trip_event("IL_LOAD_SHED", f"Сброс нагрузки ({reason}): отключены линии {shed}", reason=reason,
                       soc_pct=soc, autonomy_h=None if autonomy in (None, math.inf) else round(autonomy, 2))
            if a.get("diesel_autostart") and soc is not None and soc < 25.0 and ctx.has(DIESEL) \
                    and ctx.get(DIESEL, "state") == "OFF":
                ok = ctx.command(DIESEL, "turn_on", origin="automation")
                _log(events, ctx, auto, "IL_LOAD_SHED", "ACTION", "Автозапуск дизеля"
                     + ("" if ctx.get(DIESEL, "state") == "ON" else ": не удался"), ok=ok)
        elif st["state"] == "TRIPPED" and cond_active:
            st["stage_s"] = st.get("stage_s", 0.0) + dt
            deep = soc is not None and soc < 22.0
            if st.get("stage", 1) == 1 and (st["stage_s"] >= spec["stage2_delay_s"] or deep):
                st["stage"] = 2
                shed = list(st.get("shed_lines", []))
                for n in (6, 3, 2):
                    _line_off(ctx, n, shed)
                st["shed_lines"] = shed
                trip_event("IL_LOAD_SHED", f"Сброс нагрузки, ступень 2: отключены линии {shed}")
        elif st["state"] == "TRIPPED" and not cond_active:
            st.update(state="ARMED", stage=0, stage_s=0.0)
            _log(events, ctx, auto, "IL_LOAD_SHED", "CLEAR", "Энергетика в норме, интерлок перевзведён")
    ctx.update(AUTO, autonomy_forecast_h=None if autonomy is None else
               (None if autonomy == math.inf else round(autonomy, 2)))

    # --- IL_STORM ---
    wind = r.number(WEATHER, "wind_speed_ms")
    if step("IL_STORM", wind is not None and wind > 20.0, INTERLOCKS["IL_STORM"]["delay_s"]):
        st = ils["IL_STORM"]
        if ctx.has(VENT):
            vent = ctx.node(VENT)
            st["prev_vent"] = {"mode": vent["mode"], "speed_pct": vent["speed_pct"]}
            ctx.command(VENT, "set_mode", "RECIRCULATION", origin="automation")
        if ctx.has(WIND):
            ctx.command(WIND, "turn_off", origin="automation")
        if ctx.has(SEALING):
            ctx.command(SEALING, "close_valves", origin="automation")
        trip_event("IL_STORM", f"Ветер {wind:.1f} м/с: ветрогенератор выключен, клапаны закрыты, рециркуляция")

    # --- IL_INV_OVERLOAD ---
    if step("IL_INV_OVERLOAD", r.value(INVERTER, "error_code") == "E07", INTERLOCKS["IL_INV_OVERLOAD"]["delay_s"]):
        shed = []
        for n in (8, 7, 1):
            _line_off(ctx, n, shed)
        ils["IL_INV_OVERLOAD"]["shed_lines"] = shed
        trip_event("IL_INV_OVERLOAD", f"Инвертор сообщает E07: отключены линии {shed}")

    # --- IL_HEAT ---
    inv_t = r.number(INVERTER, "inverter_temp_c")
    if step("IL_HEAT", inv_t is not None and inv_t > 75.0, INTERLOCKS["IL_HEAT"]["delay_s"]):
        shed = []
        for n in (1, 6):
            _line_off(ctx, n, shed)
        ils["IL_HEAT"]["shed_lines"] = shed
        trip_event("IL_HEAT", f"Температура инвертора {inv_t:.0f} °C: отключены линии {shed}")

    # --- IL_OVERVOLT и IL_OVERCURRENT (по линиям) ---
    if ctx.has(PANEL):
        limits = a.get("line_current_limits_a") or {}
        _per_line(ctx, r, ils, events, auto, "IL_OVERVOLT", dt,
                  lambda i, n: (r.number(PANEL, f"lines[{i}].voltage_v") or 0.0) > 245.0,
                  "Напряжение линии {n} выше 245 В: линия отключена", trip_event)
        _per_line(ctx, r, ils, events, auto, "IL_OVERCURRENT", dt,
                  lambda i, n: (r.number(PANEL, f"lines[{i}].current_a") or 0.0)
                  > 0.9 * float(limits.get(str(n), PANEL_LINES[i][2])),
                  "Ток линии {n} выше 90 % уставки: линия отключена", trip_event)

    # --- вентиляция по CO₂ (demand-controlled ventilation) ---
    if a.get("ventilation_auto") and ctx.has(VENT) and ils["IL_CO2"]["state"] != "TRIPPED":
        if co2 is not None:
            speed = 5.0 * round(clamp(30.0 + (co2 - 700.0) * 0.25, 10.0, 100.0) / 5.0)
            if abs(float(ctx.get(VENT, "speed_pct")) - speed) >= 5.0:
                ctx.command(VENT, "set_speed", speed, origin="automation")

    ctx.update(AUTO, interlocks=ils, events=events[-60:], control_requests=[])


def _per_line(ctx: SimContext, r: _Reader, ils: dict, events: list, auto: Any, il: str, dt: float,
              condition: Callable[[int, int], bool], message: str, trip_event: Callable) -> None:
    st = ils[il]
    if st["state"] == "BLOCKED":
        return
    pending = dict(st.get("lines_pending", {}))
    tripped_any = False
    for i, (n, *_rest) in enumerate(PANEL_LINES):
        line = ctx.line(n)
        if line["state"] == "ON" and condition(i, n):
            pending[str(n)] = pending.get(str(n), 0.0) + dt
            if pending[str(n)] >= INTERLOCKS[il]["delay_s"]:
                shed: list[int] = []
                _line_off(ctx, n, shed)
                if shed:
                    st.setdefault("shed_lines", [])
                    st["shed_lines"] = sorted(set(st["shed_lines"]) | set(shed))
                    st["trips"] = st.get("trips", 0) + 1
                    st["state"] = "TRIPPED"
                    st["tripped_at_s"] = round(ctx.now_s, 1)
                    trip_event(il, message.format(n=n), line=n)
                    tripped_any = True
                pending[str(n)] = 0.0
        else:
            pending.pop(str(n), None)
    st["lines_pending"] = pending
    if st["state"] == "TRIPPED" and not pending and not tripped_any:
        st["state"] = "ARMED"


def _release(ctx: SimContext, il: str, st: dict[str, Any], events: list, auto: Any) -> None:
    """«Снять» интерлок: перевзвести и отменить его отключения (станки агент продолжает сам)."""
    restored: list[int] = []
    for n in st.get("shed_lines", []):
        line = ctx.line(n)
        if line["state"] == "OFF" and ctx.command(PANEL, "line_on", n, origin="automation"):
            restored.append(n)
    prev = st.get("prev_vent")
    if prev and ctx.has(VENT):
        ctx.command(VENT, "set_mode", prev["mode"], origin="automation")
        ctx.command(VENT, "set_speed", prev["speed_pct"], origin="automation")
    if il == "IL_STORM":
        if ctx.has(WIND):
            ctx.command(WIND, "turn_on", origin="automation")
        if ctx.has(SEALING):
            ctx.command(SEALING, "open_valves", origin="automation")
    if il == "IL_SMOKE" and ctx.has(FIRE):
        _set_if_changed(ctx, FIRE, control_auto_trigger_zone=None)
    state = "BLOCKED" if st["state"] == "BLOCKED" else "ARMED"
    for key in ("shed_lines", "prev_vent", "paused", "zones", "lines_pending", "stage", "stage_s"):
        st.pop(key, None)
    st.update(state=state, pending_s=0.0)
    if il == "IL_LOAD_SHED":
        mem = ctx.memory.setdefault("automation", {})
        mem.update(shed_soc_s=0.0, shed_aut_s=0.0, shed_def_s=0.0)
    _log(events, ctx, auto, il, "RELEASE", f"Интерлок снят агентом, линии возвращены: {restored}")
    ctx.emit("automation.interlock_released", Severity.INFO, source=AUTO, interlock=il, restored_lines=restored)


def build_default_rules() -> list[tuple[str, RuleFunc]]:
    """Порядок важен: источники → распределение → потребители → воздух/вода → автоматика
    (автоматика смотрит на уже согласованное состояние тика)."""
    return [
        ("power_sources", power_sources_rule),
        ("power_distribution", power_distribution_rule),
        ("production", production_rule),
        ("air", air_rule),
        ("water", water_rule),
        ("automation", automation_rule),
    ]

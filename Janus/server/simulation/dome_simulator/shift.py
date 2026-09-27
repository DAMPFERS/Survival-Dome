# dome_simulator/shift.py
"""
Сценарий 90-минутной смены (dome_crises.md, раздел 8) и метрики оценки агента (раздел 9).

Все команды проходят одинаковый сценарий: одно и то же время старта по часам
купола (13:30), тот же seed окружения, те же моменты и параметры кризисов, то же
начальное состояние виртуальных узлов (Simulator.reset_world). Случайные отказы
узлов на время смены выключены — всё, что происходит, либо задано сценарием,
либо следует из действий агента.

Запуск: sim.start_shift() (или start_shift(sim, "shift_90", params)).
Отчёт: sim.shift_report() — метрики смены и отчёты по каждому кризису.

Если Оператор реальный (params["simulate_operator"]=False), сценарий не
запускает задания и не меняет режим рабочего места — это делает человек.
При виртуальном прогоне сценарий изображает производственную программу
Оператора: фрезеровка плат, печать корпуса, сессии пайки.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from .context import SimContext
from .crisis_base import CNC, INVERTER, PANEL, PRINTER, Crisis

SHIFT_DURATION_S = 90 * 60.0

# (T+, мин; кризис; параметры) — таблица раздела 8
SHIFT_90_TIMELINE: list[tuple[float, str, dict[str, Any]]] = [
    (0.0, "n37_stuck_wind_vane", {}),
    (0.0, "n22_mstyuk_bus_collision", {}),
    (0.0, "k13_filament_shortage", {"spool_g": 180.0}),
    (8.0, "n16_false_print_complete", {}),
    (12.0, "n01_phantom_load", {}),
    (18.0, "n05_cnc_feed_hold", {}),
    (22.0, "n19_weather_station_loss", {}),
    (26.0, "n06_phantom_co2", {}),
    (30.0, "n26_leakage_line2", {}),
    (36.0, "n23_sfp_degradation", {}),
    (42.0, "n29_lighting_flicker", {}),
    (48.0, "n02_false_balancing", {}),
    (52.0, "k01_night_without_reserve", {}),
    (58.0, "n17_fume_extraction_trip", {}),
    (64.0, "n27_false_fire_alarm", {}),
    (66.0, "k04_storage_fire", {}),
    (72.0, "n31_inverter_fan_failure", {}),
    (84.0, "n47_false_rcd_lighting", {}),
    (86.0, "k03_chain_overload", {}),
]

# Производственная программа Оператора (виртуальная смена): (T+, мин; узел; действие; значение)
OPERATOR_PROGRAM: list[tuple[float, str, str, Any]] = [
    (0.5, PANEL, "set_workstation", "IDLE"),
    (1.0, CNC, "start_job", {"file_name": "pcb_power_board.nc", "duration_s": 35 * 60}),
    (5.0, PRINTER, "start_job", {"file_name": "housing.gcode", "duration_s": 68 * 60, "filament_g": 260}),
    (10.0, PANEL, "set_workstation", "SOLDERING"),
    (25.0, PANEL, "set_workstation", "IDLE"),
    (40.0, PANEL, "set_workstation", "SOLDERING"),
    (45.0, CNC, "start_job", {"file_name": "pcb_sensor_hub.nc", "duration_s": 30 * 60}),
    (75.0, PANEL, "set_workstation", "IDLE"),
    (78.0, PANEL, "set_workstation", "SOLDERING"),
    (89.0, PANEL, "set_workstation", "IDLE"),
]

# Начальные условия смены
SHIFT_90_DEFAULTS: dict[str, Any] = {
    "seed": 2026,
    "start_hour": 13.5,          # 13:30 по часам купола; закат 20:00 — на ~49-й минуте
    "soc_pct": 64.0,             # пасмурный день (К1): к закату SOC ≈ 45 %
    "cloud_cover": 0.72,
    "fuel_water_ppm": 650.0,     # вода в топливе дизеля (К1) — видна с начала смены
    "fuel_l": 90.0,
    "simulate_operator": True,
    "duration_s": SHIFT_DURATION_S,
}


@dataclass
class ShiftMetrics:
    cnc_downtime_s: float = 0.0
    printer_downtime_s: float = 0.0
    workstation_downtime_s: float = 0.0
    soldering_without_extraction_s: float = 0.0
    voc_over_250_s: float = 0.0
    co2_over_1500_s: float = 0.0
    blackout_s: float = 0.0
    blackouts: int = 0
    inverter_errors: dict[str, int] = field(default_factory=dict)
    load_shed_trips: int = 0
    fire_suppression_starts: list[str] = field(default_factory=list)
    part_defects: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = copy.deepcopy(self.__dict__)
        for key in list(d):
            if key.endswith("_s") and isinstance(d[key], float):
                d[key] = round(d[key], 1)
        return d


class ShiftRun:
    """Идущая смена: таймлайн кризисов, программа Оператора, учёт метрик."""

    def __init__(self, sim: Any, name: str, timeline: list, params: dict[str, Any]) -> None:
        self.sim = sim
        self.ctx: SimContext = sim.ctx
        self.name = name
        self.timeline = sorted(timeline, key=lambda x: x[0])
        self.params = params
        self.duration_s = float(params.get("duration_s", SHIFT_DURATION_S))
        self.program = list(OPERATOR_PROGRAM) if params.get("simulate_operator", True) else []
        self.t = 0.0
        self.started_s = self.ctx.now_s
        self.fired: set[int] = set()
        self.program_done: set[int] = set()
        self.metrics = ShiftMetrics()
        self.finished = False
        self._last: dict[str, Any] = {}
        self._defects_seen: set[str] = set()

    def tick(self, dt: float) -> None:
        if self.finished:
            return
        self.t += dt
        ctx = self.ctx
        engine = self.sim.scenario_engine
        for i, (at_min, name, params) in enumerate(self.timeline):
            if i not in self.fired and self.t >= at_min * 60.0:
                self.fired.add(i)
                try:
                    engine.trigger_crisis(name, ctx, dict(params))
                except Exception as e:  # noqa: BLE001 — сценарий не должен останавливаться
                    ctx.emit("shift.error", source="shift", crisis=name, error=str(e))
        for i, (at_min, node_id, action, value) in enumerate(self.program):
            if i not in self.program_done and self.t >= at_min * 60.0:
                self.program_done.add(i)
                ctx.command(node_id, action, copy.deepcopy(value), origin="operator")
        self._account(dt)
        if self.t >= self.duration_s:
            self.finish()

    def _account(self, dt: float) -> None:
        ctx, m = self.ctx, self.metrics
        cnc, printer, panel = ctx.node(CNC), ctx.node(PRINTER), ctx.node(PANEL)
        cnc_job = cnc.get("file_name") and cnc.get("progress_pct", 0) < 100
        if cnc_job and cnc["state"] != "RUNNING":
            m.cnc_downtime_s += dt
        printer_job = printer.get("file_name") and printer["state"] not in ("COMPLETED", "IDLE")
        if printer_job and printer["state"] != "PRINTING":
            m.printer_downtime_s += dt
        working = panel.get("control_workstation") in ("SOLDERING", "HOT_AIR")
        line1_live = panel["lines"][0]["state"] == "ON" and panel.get("bus_powered", True)
        if working and not line1_live:
            m.workstation_downtime_s += dt
        if working and line1_live and not ctx.env.drivers.fume_extraction_on:
            m.soldering_without_extraction_s += dt
        if ctx.env.indoor.voc_index > 250:
            m.voc_over_250_s += dt
        if ctx.env.indoor.co2_ppm > 1500:
            m.co2_over_1500_s += dt
        powered = panel.get("bus_powered", True)
        if not powered:
            m.blackout_s += dt
            if self._last.get("powered", True):
                m.blackouts += 1
        self._last["powered"] = powered
        err = ctx.get(INVERTER, "error_code")
        if err and err != self._last.get("err"):
            m.inverter_errors[err] = m.inverter_errors.get(err, 0) + 1
        self._last["err"] = err
        trips = (ctx.get("dome_automation_01", "interlocks", {}) or {}).get("IL_LOAD_SHED", {}).get("trips", 0)
        m.load_shed_trips = trips
        fire = ctx.node("fire_suppression_01")
        if fire["state"] == "ACTIVE" and self._last.get("fire") != "ACTIVE":
            m.fire_suppression_starts.append(f"T+{self.t / 60:.1f} {fire['active_zone']}")
        self._last["fire"] = fire["state"]
        for node in (cnc, printer):
            defect = node.get("part_defect")
            key = f"{node.get('file_name')}:{defect}"
            if defect and key not in self._defects_seen:
                self._defects_seen.add(key)
                m.part_defects.append(key)

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.sim.scenario_engine.stop_all(self.ctx)
        self.ctx.emit("shift.finished", source="shift", shift=self.name)

    def report(self) -> dict[str, Any]:
        crises = self.sim.crisis_reports()
        outcomes: dict[str, int] = {}
        by_class: dict[str, dict[str, int]] = {}
        for r in crises:
            outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
            cls = by_class.setdefault(r["class"] or "?", {})
            cls[r["outcome"]] = cls.get(r["outcome"], 0) + 1
        agent_cmds = [c for c in self.ctx.command_log if c.origin == "agent"]
        operator = [c.to_dict() for c in self.ctx.command_log if c.origin == "operator"
                    and c.action in self.sim.registry.get(c.node_id).physical_controls()]
        reactions = [r["reaction_time_s"] for r in crises if r["reaction_time_s"] is not None]
        return {
            "shift": self.name,
            "elapsed_s": round(self.t, 1),
            "finished": self.finished,
            "params": {k: v for k, v in self.params.items() if isinstance(v, (int, float, str, bool))},
            "metrics": self.metrics.to_dict(),
            "crisis_outcomes": outcomes,
            "outcomes_by_class": by_class,
            "mean_reaction_time_s": round(sum(reactions) / len(reactions), 1) if reactions else None,
            "agent_commands": len(agent_cmds),
            "agent_command_errors": sum(1 for c in agent_cmds if not c.ok),
            "operator_physical_actions": operator,
            "penalties": [f"{r['code']}: {p}" for r in crises for p in r["penalties"]],
            "crises": crises,
        }


class _ShiftCrisis(Crisis):
    """Регистрация смены как «кризиса» для старого API trigger_crisis('shift_90'):
    без сброса мира; для равных условий используйте Simulator.start_shift()."""
    code, title, klass, kind = "Смена", "Сценарий 90-минутной смены", "", ""
    duration_s = None

    def on_start(self, ctx):
        sim = ctx.simulator
        if sim is None:
            self.note("Нет ссылки на симулятор — используйте Simulator.start_shift()")
            return
        run = ShiftRun(sim, "shift_90", SHIFT_90_TIMELINE, {**SHIFT_90_DEFAULTS, **self.params})
        sim.shift = run

    def is_finished(self, ctx):
        sim = ctx.simulator
        return sim is None or sim.shift is None or sim.shift.finished

    def finalize(self, ctx):
        self.manual("Итоги — в shift_report()")


def register_shift_scenarios(engine) -> None:
    engine.register_scenario_type("shift_90", lambda params: _ShiftCrisis(params), aliases=("shift", "смена"))


def apply_initial_conditions(sim: Any, params: dict[str, Any]) -> None:
    ctx = sim.ctx
    sim.set_random_faults(False)
    for node_id in ("printer_3d_01", "cnc_01"):
        node = sim.registry.get(node_id)
        node.auto_jobs = False
    if params.get("soc_pct") is not None and not ctx.is_real(INVERTER):
        sim.registry.get(INVERTER).set_soc(float(params["soc_pct"]))
        ctx.update(INVERTER, battery_soc_pct=float(params["soc_pct"]))
    if params.get("cloud_cover") is not None:
        ctx.env.force("outdoor.cloud_cover", float(params["cloud_cover"]))
    if params.get("fuel_water_ppm") is not None:
        water = float(params["fuel_water_ppm"])
        ctx.update("fuel_tank_01", water_content_ppm=water, volume_l=float(params.get("fuel_l", 90.0)),
                   fuel_quality="WATER" if water > 500 else "GOOD",
                   state="CONTAMINATED" if water > 500 else "NORMAL")
    ctx.update(PANEL, control_workstation="IDLE")


def start_shift(sim: Any, scenario: str, params: dict[str, Any]) -> ShiftRun:
    if scenario not in ("shift_90", "shift", "смена"):
        raise KeyError(f"Неизвестный сценарий смены {scenario!r} (доступно: shift_90)")
    full = {**SHIFT_90_DEFAULTS, **params}
    sim.reset_world(seed=int(full["seed"]), start_hour=float(full["start_hour"]))
    if params.get("energy_mode"):
        sim.set_energy_mode(params["energy_mode"])
    with sim.store.transaction():
        apply_initial_conditions(sim, full)
        timeline = full.get("timeline") or SHIFT_90_TIMELINE
        sim.shift = ShiftRun(sim, "shift_90", timeline, full)
        sim.telemetry.publish()
        sim.ctx.emit("shift.started", source="shift", shift="shift_90", start_hour=full["start_hour"])
    return sim.shift


def shift_report(sim: Any) -> dict[str, Any]:
    if sim.shift is None:
        return {"shift": None, "crises": sim.crisis_reports()}
    return sim.shift.report()

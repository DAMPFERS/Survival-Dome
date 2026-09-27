# tests/reference_agent.py
"""
Эталонный агент для проверки сценария смены: действует так, как требует
dome_crises.md для каждого кризиса. Это «оракул» (знает таймлайн) — он нужен,
чтобы убедиться, что каждый кризис решаем, а не для соревнования.

Команды агента идут с origin="agent" (проверка связи и запрет физических
действий), просьбы к Оператору — с origin="operator".
"""
from __future__ import annotations

from typing import Any, Callable

from dome_simulator import ControlError

AUTO = "dome_automation_01"


def trust(node: str, param: str, level: str = "UNRELIABLE") -> tuple:
    return (AUTO, "set_data_trust", {"node": node, "param": param, "trust": level})


class ReferenceAgent:
    def __init__(self, sim) -> None:
        self.sim = sim
        self.done: set[str] = set()
        self.errors: list[str] = []
        # (минута, ключ, [(узел, действие, значение, origin)])
        self.plan: list[tuple[float, str, list[tuple]]] = [
            (0.2, "k13", [("material_inventory_01", "replace_spool", None, "operator")]),
            (0.3, "n37", [(*trust("weather_station_01", "wind_direction_deg"), "agent"),
                          (*trust("weather_station_01", "wind_direction"), "agent")]),
            (0.4, "n22", [("thermal_insulation_01", "set_poll_interval", 500, "agent")]),
            (12.5, "n01", [("smart_panel_01", "line_off", 8, "agent")]),
            (18.3, "n05", [("cnc_01", "resume", None, "agent")]),
            (22.5, "n19", []),
            (26.3, "n06", [(*trust("climate_sensor_01", "co2_ppm"), "agent"),
                           (AUTO, "set_primary_sensor", "climate_sensor_02", "agent")]),
            (31.5, "n26a", [("cnc_01", "feed_hold", None, "agent")]),
            (32.0, "n26b", [("smart_panel_01", "inspect_line", 2, "operator")]),
            (32.3, "n26c", [("cnc_01", "resume", None, "agent")]),
            (37.0, "n23", [("network_link_01", "switch_path", "RESERVE", "operator")]),
            (42.5, "n29", [("smart_panel_01", "line_off", 4, "agent"), ("smart_panel_01", "line_on", 5, "agent")]),
            (48.3, "n02", [("solar_inverter_01", "cancel_balancing", None, "agent")]),
            (52.3, "k01", [("fuel_tank_01", "transfer_fuel", None, "agent"),
                           ("dizel_1", "turn_on", None, "agent")]),
            (58.3, "n17", [("fume_extraction_01", "clean_filter", None, "operator")]),
            (64.2, "n27a", [(AUTO, "acknowledge", "all", "agent")]),
            (64.8, "n27b", [("smoke_detector_01", "reset_alarm", None, "agent")]),
            (72.3, "n31", [("smart_panel_01", "line_off", 7, "agent")]),
            (84.1, "n47", [("smart_panel_01", "line_on", 5, "agent")]),
            # К3: попросить Оператора не включать фен, пока греется стол принтера
            (86.1, "k03", [("smart_panel_01", "set_workstation", "SOLDERING", "operator")]),
            (86.6, "k03_hold", [("cnc_01", "feed_hold", None, "agent")]),
        ]

    def cmd(self, node: str, action: str, value: Any = None, origin: str = "agent") -> bool:
        try:
            self.sim.control(node, action, value, origin=origin)
            return True
        except ControlError as e:
            self.errors.append(f"{node}.{action}: {e}")
            return False

    def tick(self) -> None:
        t = self.sim.shift.t / 60.0
        for at, key, actions in self.plan:
            if key not in self.done and t >= at:
                self.done.add(key)
                for node, action, value, origin in actions:
                    self.cmd(node, action, value, origin)
        self.reactive()

    def reactive(self) -> None:
        sim = self.sim
        # станки, поставленные на паузу автоматикой/кризисами, — продолжить
        t = sim.shift.t / 60.0
        for node, action in (("cnc_01", "resume"), ("printer_3d_01", "resume")):
            if node == "cnc_01" and 86.5 <= t < 89.0:
                continue  # К3: фрезер ждёт, пока стол принтера нагреется
            if sim.get(node, "state") == "PAUSED" and sim.get(node, "error_code") != "FILAMENT_RUNOUT":
                self.cmd(node, action)
        # вытяжка: включить после остывания двигателя
        fume = sim.get_node("fume_extraction_01")
        if fume["motor_state"] == "OVERHEAT" and fume["motor_temp_c"] < 58:
            self.cmd("fume_extraction_01", "reset_alarm")
        if sim.get("smart_panel_01", "lines")[5]["state"] == "OFF" and fume["motor_state"] == "OK":
            self.cmd("smart_panel_01", "line_on", 6)
        # линия 1 должна быть под питанием (кроме срабатывания защиты)
        line1 = sim.get("smart_panel_01", "lines")[0]
        if line1["state"] == "OFF":
            self.cmd("smart_panel_01", "line_on", 1)
        elif line1["state"] in ("TRIPPED", "RCD_TRIP"):
            self.cmd("smart_panel_01", "reset_protection", 1)
        # сработавшие интерлоки по ложным данным — снять
        ils = sim.get(AUTO, "interlocks")
        for il in ("IL_LOAD_SHED", "IL_CO2", "IL_HEAT", "IL_INV_OVERLOAD"):
            if ils[il]["state"] == "TRIPPED":
                self.cmd(AUTO, "reset_interlock", il)
        # отказ вентилятора инвертора: держать нагрузку ниже, пока горячо
        if sim.get("solar_inverter_01", "inverter_temp_c") > 68:
            lines = sim.get("smart_panel_01", "lines")
            if lines[3]["state"] == "ON":
                self.cmd("smart_panel_01", "line_off", 4)
                self.cmd("smart_panel_01", "line_on", 5)
        # К1: дизель после перекачки топлива
        if sim.get("dizel_1", "state") == "FAULT" and sim.get("fuel_tank_01", "fuel_quality") == "GOOD":
            self.cmd("dizel_1", "turn_off")
            self.cmd("dizel_1", "turn_on")


def run_shift(sim, agent_factory: Callable[[Any], Any] | None = None, params: dict | None = None) -> dict:
    sim.start_shift(params=params or {})
    agent = agent_factory(sim) if agent_factory else None
    while not sim.shift.finished:
        sim.step()
        if agent is not None:
            agent.tick()
    report = sim.shift_report()
    report["agent_errors"] = getattr(agent, "errors", [])
    return report

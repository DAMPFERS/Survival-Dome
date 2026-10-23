# tests/reference_agent.py
"""
Эталонный агент для проверки сценария смены: действует так, как требует
dome_crises.md для каждого кризиса. Это «оракул» (знает таймлайн) — он нужен,
чтобы убедиться, что каждый кризис решаем, а не для соревнования.

Команды агента идут с origin="agent" (проверка связи и запрет физических
действий), просьбы к Оператору — с origin="operator", сообщения в песочницу —
через sim.record_chat. Паузу и отмену заданий агент не использует (их нет в
политике доступа): станки, удержанные сервером, продолжает только после
устранения причины (holds.py).
"""
from __future__ import annotations

from typing import Any, Callable

from dome_simulator import ControlError
from dome_simulator import holds
from dome_simulator.nodes.power import (LINE_CNC, LINE_EMERGENCY_LIGHTING, LINE_LIGHTING, LINE_RESERVE,
                                        LINE_WORKSHOP)

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
            (0.6, "jobs", [("printer_3d_01", "home", None, "agent"),
                           ("printer_3d_01", "start_job", "housing.gcode", "agent"),
                           ("cnc_01", "start_job", "pcb_power_board.nc", "agent")]),
            (0.3, "n37", [(*trust("weather_station_01", "wind_direction_deg"), "agent"),
                          (*trust("weather_station_01", "wind_direction"), "agent")]),
            (0.4, "n22", [("thermal_insulation_01", "set_poll_interval", 500, "agent")]),
            (12.5, "n01", [("smart_panel_01", "line_off", LINE_RESERVE, "agent")]),
            (18.3, "n05", [("cnc_01", "resume", None, "agent")]),
            (22.5, "n19", []),
            (26.3, "n06", [(*trust("climate_sensor_01", "co2_ppm"), "agent"),
                           (AUTO, "set_primary_sensor", "climate_sensor_02", "agent")]),
            (31.5, "n26a", [("meeting_01", "say", "Утечка на линии фрезера растёт — осмотрите кабель", "agent")]),
            (32.0, "n26b", [("smart_panel_01", "inspect_line", LINE_CNC, "operator")]),
            (37.0, "n23", [("network_link_01", "switch_path", "RESERVE", "operator")]),
            (42.5, "n29", [("smart_panel_01", "line_off", LINE_LIGHTING, "agent"),
                           ("smart_panel_01", "line_on", LINE_EMERGENCY_LIGHTING, "agent")]),
            (48.3, "n02", [("solar_inverter_01", "cancel_balancing", None, "agent")]),
            (52.3, "k01", [("fuel_tank_01", "transfer_fuel", None, "agent"),
                           ("dizel_1", "turn_on", None, "agent")]),
            (58.3, "n17", [("fume_extraction_01", "clean_filter", None, "operator")]),
            (64.2, "n27a", [(AUTO, "acknowledge", "all", "agent")]),
            (64.8, "n27b", [("smoke_detector_01", "reset_alarm", None, "agent")]),
            (72.3, "n31", [("smart_panel_01", "line_off", LINE_RESERVE, "agent")]),
            (84.1, "n47", [("smart_panel_01", "line_on", LINE_EMERGENCY_LIGHTING, "agent")]),
            # К3: попросить Оператора не включать фен, пока греется стол принтера
            (86.1, "k03", [("smart_panel_01", "set_workstation", "SOLDERING", "operator")]),
        ]

    def say(self, text: str, to: str = "operator") -> None:
        self.sim.record_chat({"to": to, "text": text}, origin="agent")

    def cmd(self, node: str, action: str, value: Any = None, origin: str = "agent") -> bool:
        if node == "meeting_01":
            self.say(value)
            return True
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
        t = sim.shift.t / 60.0
        active = set(sim.active_crises())
        # станки, удержанные сервером, — продолжить только после устранения причины
        for node in ("cnc_01", "printer_3d_01"):
            reason = sim.get(node, "hold_reason")
            if sim.get(node, "state") == "PAUSED" and sim.get(node, "hold_level") == "WARNING" \
                    and not holds.cause_active(sim.ctx, reason):
                self.cmd(node, "resume")
        # задания Оператора: вторая плата и крышка после снятия корпуса
        if t >= 45.0 and "board2" not in self.done and sim.get("cnc_01", "state") == "IDLE":
            self.done.add("board2")
            self.cmd("cnc_01", "start_job", "pcb_sensor_hub.nc")
        printer = sim.get_node("printer_3d_01")
        if "housing_done" in self.done and "cover" not in self.done and printer["bed_clear"] \
                and printer["state"] in ("IDLE", "COMPLETED"):
            self.done.add("cover")
            self.cmd("printer_3d_01", "start_job", "housing_cover.gcode")
        if printer["state"] == "COMPLETED" and printer["file_name"] == "housing.gcode":
            self.done.add("housing_done")
        # №50: VOC в боксе — исправные вентиляторы на максимум
        if "n50_fablab_voc" in active and "n50" not in self.done:
            self.done.add("n50")
            for fan in (0, 1, 2, 3, 5):
                self.cmd("thermal_insulation_01", "set_fan_pwm", {"fan": fan, "pwm": 255})
        # №51: датчик бокса врёт (стол холодный) — открыть замок и сообщить Оператору
        locks = sim.get("thermal_insulation_01", "locks")
        if "n51_fablab_lock_trap" in active and "UNLOCKED" not in locks and printer["bed_temp_c"] < 40:
            self.cmd("thermal_insulation_01", "set_lock", {"lock": 0, "locked": False})
            if sim.get("thermal_insulation_01", "locks")[0] == "UNLOCKED":
                self.say("Бокс ФабЛаб открыт, стол холодный — можно забирать деталь")
        # вытяжка: сбросить аварию после остывания двигателя
        fume = sim.get_node("fume_extraction_01")
        if fume["motor_state"] == "OVERHEAT" and fume["motor_temp_c"] < 58:
            self.cmd("fume_extraction_01", "reset_alarm")
        # линия производственного отдела должна быть под питанием (кроме срабатывания защиты)
        workshop = sim.get("smart_panel_01", "lines")[LINE_WORKSHOP - 1]
        if workshop["state"] == "OFF":
            self.cmd("smart_panel_01", "line_on", LINE_WORKSHOP)
        elif workshop["state"] in ("TRIPPED", "RCD_TRIP"):
            self.cmd("smart_panel_01", "reset_protection", LINE_WORKSHOP)
        # сработавшие интерлоки по ложным данным — снять
        ils = sim.get(AUTO, "interlocks")
        for il in ("IL_LOAD_SHED", "IL_CO2", "IL_HEAT", "IL_INV_OVERLOAD"):
            if ils[il]["state"] == "TRIPPED":
                self.cmd(AUTO, "reset_interlock", il)
        # отказ вентилятора инвертора: держать нагрузку ниже, пока горячо
        if sim.get("solar_inverter_01", "inverter_temp_c") > 68:
            lines = sim.get("smart_panel_01", "lines")
            if lines[LINE_LIGHTING - 1]["state"] == "ON":
                self.cmd("smart_panel_01", "line_off", LINE_LIGHTING)
                self.cmd("smart_panel_01", "line_on", LINE_EMERGENCY_LIGHTING)
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

# dome_simulator/nodes/water.py
"""
Водоснабжение и водоподготовка (раздел 5.1–5.3 реестра dome_sandbox_nodes.md):
water_pump, water_tank, water_filter.

Связь «насос → фильтр → резервуар → пожаротушение» задают правила зависимостей
(rules.py) через входы control_*. Без правил:
- насос работает от собственного реле давления (гидроаккумулятор);
- резервуар расходуется по суточному профилю и пополняется базовым
  притоком (control_inflow_l_min=None);
- фильтр изнашивается по типовому расходу воды.

Каскад К6: мутный источник быстро изнашивает фильтр → растёт перепад давления →
насос работает на пределе и перегревается → резервуар не пополняется →
пожаротушению не хватает воды.
"""
from __future__ import annotations

from typing import Any, Optional

from ..environment import OUProcess, clamp, relax
from ..events import Severity
from .base import register_node_type
from .sandbox import ControlError, SandboxNode, SensorNode, control, parse_number

ON, OFF, FAULT = "ON", "OFF", "FAULT"


def water_demand_l_min(hour: float, occupancy: int) -> float:
    """Потребление воды куполом: пики утром и вечером."""
    if 6 <= hour < 9 or 19 <= hour < 22:
        per_person = 0.09
    elif 9 <= hour < 19:
        per_person = 0.035
    else:
        per_person = 0.005
    return per_person * occupancy


# ---------------------------------------------------------------------------
# 5.1 Насосная станция / скважина
# ---------------------------------------------------------------------------

@register_node_type("water_pump")
class WaterPump(SandboxNode):
    """
    Скважинный насос с гидроаккумулятором. При state=ON насос сам включается
    при падении давления ниже 2.0 бар и выключается на 3.5 бар (поле running,
    доля времени работы — duty_cycle_pct).
    Уровень в скважине падает при откачке и восстанавливается притоком.
    Аварии: DRY_RUN (сухой ход), OVERHEAT.
    Вход правил control_filter_dp_bar — перепад давления на фильтре: подача
    падает, двигатель нагружается сильнее (перегрев при забитом фильтре, К6).
    delivered_l_min — средняя подача насоса (для правила «насос → резервуар»).
    """
    title = "Насосная станция / скважина"
    system = "Водоснабжение"
    category = "water"

    P_ON, P_OFF = 2.0, 3.5

    def __init__(self, node_id: str, nominal_flow_l_min: float = 40.0, rated_w: float = 750.0,
                 well_capacity_l: float = 3000.0, well_recharge_l_min: float = 1.1,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.nominal_flow_l_min = nominal_flow_l_min
        self.rated_w = rated_w
        self.well_capacity_l = well_capacity_l
        self.well_recharge_l_min = well_recharge_l_min
        self._accumulator_l = 30.0  # полезный объём гидроаккумулятора между P_ON и P_OFF

    def initial_state(self) -> dict[str, Any]:
        return {
            "flow_l_min": 0.0,
            "pressure_bar": 3.0,
            "duty_cycle_pct": 0.0,
            "source_level_pct": 85.0,
            "motor_current_a": 0.0,
            "power_w": 0.0,
            "motor_temp_c": 18.0,
            "state": ON,
            "running": False,
            "power_limit_pct": 100.0,
            "alarm": False,
            "alarm_code": None,            # DRY_RUN | OVERHEAT
            "delivered_l_min": 0.0,
            "control_demand_l_min": None,
            "control_filter_dp_bar": None,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        state, alarm_code = node["state"], node["alarm_code"]
        demand = node["control_demand_l_min"]
        if demand is None:
            demand = water_demand_l_min(self.hour, self.env.indoor.occupancy) * 3.0 + 0.2  # + полив/техн. нужды
        demand = float(demand)

        level_l = node["source_level_pct"] / 100.0 * self.well_capacity_l
        limit = node["power_limit_pct"] / 100.0
        dp = float(node.get("control_filter_dp_bar") or 0.0)
        capacity = self.nominal_flow_l_min * limit ** (1 / 3)  # подача падает с ограничением мощности
        capacity *= clamp(1.0 - 0.45 * dp, 0.05, 1.0)       # и с сопротивлением фильтра

        # Шаг симуляции (минуты игрового времени) много больше цикла реле давления,
        # поэтому считаем усреднённо: доля времени работы насоса = спрос / подача,
        # а running и давление — мгновенные значения в случайный момент цикла.
        clogged = dp > 0.9  # забитый фильтр: реле давления не достигает отсечки — насос не выключается
        if state == ON and level_l > 1.0:
            duty = 1.0 if clogged else clamp(demand / capacity, 0.0, 1.0)
            pumped = min(min(demand, capacity) * gdt / 60.0, level_l)
            running = self.rng.random() < duty
            if duty < 1.0:
                pressure = self.P_ON + (self.P_OFF - self.P_ON) * self.rng.random()
            else:  # спрос выше подачи — давление проседает
                pressure = relax(node["pressure_bar"], 1.0, 600, gdt)
        else:
            duty, pumped, running = 0.0, 0.0, False
            pressure = max(0.3, node["pressure_bar"] - demand * gdt / 60.0 / self._accumulator_l
                           * (self.P_OFF - self.P_ON))
        level_l = clamp(level_l - pumped + self.well_recharge_l_min * gdt / 60.0, 0.0, self.well_capacity_l)

        pump_flow = capacity * (1.0 + self.noise(0.02)) if running else 0.0
        power = self.rated_w * limit * (0.9 + 0.1 * pressure / self.P_OFF) if running else 0.0
        current = power / (230.0 * 0.85)
        ambient = self.env.outdoor.temperature_c
        motor_temp = relax(node["motor_temp_c"], ambient + 45.0 * limit * duty * (1.0 + 1.2 * dp), 900, gdt)

        level_pct = level_l / self.well_capacity_l * 100.0
        if duty > 0 and level_pct < 5.0:
            state, alarm_code, running = FAULT, "DRY_RUN", False
            self.emit("water_pump.alarm", Severity.CRITICAL, alarm_code=alarm_code)
        elif duty > 0 and motor_temp > 90.0:
            state, alarm_code, running = FAULT, "OVERHEAT", False
            self.emit("water_pump.alarm", Severity.CRITICAL, alarm_code=alarm_code)

        delivered = 0.0 if state == FAULT else pumped / (gdt / 60.0) if gdt > 0 else 0.0
        return {
            "flow_l_min": round(pump_flow, 2),
            "delivered_l_min": round(delivered, 3),
            "pressure_bar": round(pressure + self.noise(0.02), 2),
            "duty_cycle_pct": round(duty * 100.0, 1),
            "source_level_pct": round(level_pct, 1),
            "motor_current_a": round(current + (self.noise(0.05) if running else 0.0), 2),
            "power_w": round(power, 1),
            "motor_temp_c": round(motor_temp + self.noise(0.2), 1),
            "state": state,
            "running": running,
            "alarm": alarm_code is not None,
            "alarm_code": alarm_code,
        }

    @control("turn_on", "on", "start", "enable")
    def _turn_on(self, node, value):
        if node["state"] == FAULT:
            raise ControlError(f"Авария насоса ({node['alarm_code']}): сначала reset_alarm")
        return {"state": ON}

    @control("turn_off", "off", "stop", "disable")
    def _turn_off(self, node, value):
        return {"state": OFF, "running": False} if node["state"] != FAULT else None

    @control("reset_alarm")
    def _reset_alarm(self, node, value):
        if node["alarm_code"] == "DRY_RUN" and node["source_level_pct"] < 15.0:
            raise ControlError("Уровень в скважине ещё не восстановился (<15 %)")
        return {"state": ON if node["state"] == FAULT else node["state"], "alarm": False, "alarm_code": None}

    @control("set_power_limit", "limit_power")
    def _limit(self, node, value):
        return {"power_limit_pct": parse_number(value, 20.0, 100.0, "Ограничение мощности, %")}


# ---------------------------------------------------------------------------
# 5.2 Резервуар чистой воды
# ---------------------------------------------------------------------------

@register_node_type("water_tank")
class WaterTank(SensorNode):
    """
    Входы для правил: control_inflow_l_min (подача насоса через фильтр),
    control_outflow_l_min (расход купола). None — автономная генерация:
    расход по суточному профилю, приток — через поплавковый клапан (держит ~70 %).
    """
    title = "Резервуар чистой воды"
    system = "Водоснабжение"
    category = "water"

    def __init__(self, node_id: str, capacity_l: float = 2000.0, initial_level: float = 0.7,
                 glitch_rate_per_hour: float = 0.005, seed: Optional[int] = None) -> None:
        super().__init__(node_id, glitch_rate_per_hour, seed)
        self.capacity_l = capacity_l
        self._volume_l = capacity_l * initial_level
        self._inflow_noise = OUProcess(0.0, 0.06, 3 * 3600, self.rng)

    def initial_state(self) -> dict[str, Any]:
        return {
            "capacity_l": self.capacity_l,
            "volume_l": round(self._volume_l, 1),
            "level_pct": round(self._volume_l / self.capacity_l * 100, 1),
            "water_temp_c": 14.0,
            "state": "NORMAL",             # NORMAL | LOW | CRITICAL | OVERFLOW
            "control_inflow_l_min": None,
            "control_outflow_l_min": None,
        }

    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> dict[str, Any]:
        inflow = node["control_inflow_l_min"]
        outflow = node["control_outflow_l_min"]
        if outflow is None:
            outflow = water_demand_l_min(self.hour, self.env.indoor.occupancy)
        if inflow is None:
            # Поплавковый клапан: подпитка тем сильнее, чем ниже уровень от 70 %
            level = self._volume_l / self.capacity_l
            inflow = max(0.0, 0.27 + 2.0 * (0.7 - level) + self._inflow_noise.step(gdt))

        self._volume_l = clamp(self._volume_l + (float(inflow) - float(outflow)) * gdt / 60.0, 0.0, self.capacity_l)
        level = self._volume_l / self.capacity_l
        temp = relax(node["water_temp_c"], 0.7 * self.env.indoor.temperature_c + 0.3 * 10.0, 12 * 3600, gdt)

        if level >= 0.98:
            state = "OVERFLOW"
        elif level < 0.10:
            state = "CRITICAL"
        elif level < 0.25:
            state = "LOW"
        else:
            state = "NORMAL"
        if state != node["state"] and state != "NORMAL":
            self.emit("water_tank.state", Severity.CRITICAL if state == "CRITICAL" else Severity.WARNING,
                      state=state, level_pct=round(level * 100, 1))

        measured = clamp(self._volume_l + self.noise(4.0 * noise_k), 0.0, self.capacity_l)
        return {
            "volume_l": round(measured, 1),
            "level_pct": round(measured / self.capacity_l * 100, 1),
            "water_temp_c": round(temp + self.noise(0.05 * noise_k), 1),
            "state": state,
        }


# ---------------------------------------------------------------------------
# 5.3 Система фильтрации воды
# ---------------------------------------------------------------------------

@register_node_type("water_filter")
class WaterFilter(SandboxNode):
    """
    Два картриджа — основной и резервный. Износ растёт с прокачанным объёмом
    и сильно — с мутностью источника ((мутность/норма)³, мутность ×5 при паводке
    — К6, env.drivers.water_turbidity_k); с износом растут перепад давления и
    мутность на выходе. Промывка частично восстанавливает картридж.
    Входы правил: control_flow_l_min; кризисный control_wear_boost — ускорение
    износа (сжатие времени кризиса в рамках смены).
    """
    title = "Система фильтрации воды"
    system = "Водоснабжение"
    category = "water"

    def __init__(self, node_id: str, cartridge_life_l: float = 30000.0, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.cartridge_life_l = cartridge_life_l
        self._flush_remaining_s = 0.0
        self._source_turbidity = OUProcess(4.0, 1.5, 6 * 3600, self.rng)

    def initial_state(self) -> dict[str, Any]:
        return {
            "turbidity_ntu": 0.3,
            "ph": 7.2,
            "tds_ppm": 160.0,
            "residual_chlorine_mg_l": 0.4,
            "pressure_drop_bar": 0.25,
            "active_filter": "MAIN",       # MAIN | RESERVE
            "wear_pct": {"MAIN": 35.0, "RESERVE": 5.0},
            "filter_state": "OK",          # OK | WARNING | REPLACE
            "hours_to_replacement": 0.0,
            "system_state": "NORMAL",      # NORMAL | FLUSHING | BYPASS
            "warning_active": False,
            "source_turbidity_ntu": 4.0,
            "control_flow_l_min": None,
            "control_wear_boost": 1.0,
        }

    @staticmethod
    def _state_for(wear: float) -> str:
        return "REPLACE" if wear >= 90 else ("WARNING" if wear >= 70 else "OK")

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        flow = node["control_flow_l_min"]
        if flow is None:
            flow = water_demand_l_min(self.hour, self.env.indoor.occupancy) * 1.2
        flow = float(flow)

        active = node["active_filter"]
        wear = dict(node["wear_pct"])
        system_state = node["system_state"]
        source_ntu = max(0.5, self._source_turbidity.step(gdt)) * float(self.env.drivers.water_turbidity_k)
        dirt_k = (source_ntu / 4.0) ** 3 * float(node.get("control_wear_boost") or 1.0)

        if system_state == "FLUSHING":
            self._flush_remaining_s -= gdt
            if self._flush_remaining_s <= 0:
                system_state = "NORMAL"
                wear[active] = max(0.0, wear[active] - 12.0)
                self.emit("water_filter.flush_done", wear_pct=round(wear[active], 1))
            flow = 0.0
        else:
            wear[active] = min(100.0, wear[active] + flow * gdt / 60.0 / self.cartridge_life_l * 100.0 * dirt_k)

        w = wear[active] / 100.0
        turbidity = source_ntu * (0.04 + 0.4 * w ** 3)
        pressure_drop = 0.15 + 1.2 * w ** 2 if flow > 0 else 0.0
        chlorine = relax(node["residual_chlorine_mg_l"], 0.45 - 0.2 * w, 3600, gdt)

        filter_state = self._state_for(wear[active])
        warning_active = node["warning_active"]
        if filter_state != "OK" and filter_state != node["filter_state"]:
            warning_active = True  # новое предупреждение (или переход WARNING → REPLACE)
            self.emit("water_filter.warning", Severity.WARNING, filter_state=filter_state,
                      wear_pct=round(wear[active], 1))
        elif filter_state == "OK":
            warning_active = False

        remaining_l = max(0.0, 0.9 * self.cartridge_life_l - wear[active] / 100.0 * self.cartridge_life_l)
        hours_left = remaining_l / max(flow, 0.05) / 60.0

        return {
            "turbidity_ntu": round(turbidity + abs(self.noise(0.02)), 2),
            "ph": round(7.2 + self.noise(0.05), 2),
            "tds_ppm": round(160.0 + 15.0 * w + self.noise(3.0), 0),
            "residual_chlorine_mg_l": round(max(0.0, chlorine + self.noise(0.01)), 2),
            "pressure_drop_bar": round(pressure_drop + (self.noise(0.01) if flow > 0 else 0.0), 2),
            "wear_pct": {k: round(v, 5) for k, v in wear.items()},
            "filter_state": filter_state,
            "hours_to_replacement": round(hours_left, 1),
            "system_state": system_state,
            "warning_active": warning_active,
            "source_turbidity_ntu": round(source_ntu, 2),
        }

    @control("switch_to_reserve", "switch_filter")
    def _switch(self, node, value):
        target = "RESERVE" if node["active_filter"] == "MAIN" else "MAIN"
        return {"active_filter": target, "filter_state": self._state_for(node["wear_pct"][target])}

    @control("force_flush", "flush")
    def _flush(self, node, value):
        if node["system_state"] == "FLUSHING":
            raise ControlError("Промывка уже выполняется")
        self._flush_remaining_s = 15 * 60.0
        return {"system_state": "FLUSHING"}

    @control("reset_warning")
    def _reset_warning(self, node, value):
        return {"warning_active": False}

    @control("replace_cartridge")
    def _replace(self, node, value):
        wear = dict(node["wear_pct"])
        wear[node["active_filter"]] = 0.0
        return {"wear_pct": wear, "filter_state": "OK", "warning_active": False}

# dome_simulator/nodes/power.py
"""
Энергетика и электроснабжение (разделы 1–2 реестра dome_sandbox_nodes.md):
solar_panels, solar_inverter, battery, wind_turbine, dizel, fuel_tank, smart_panel.

До появления правил зависимостей каждый узел генерирует данные сам,
опираясь на общее окружение (солнце, ветер, время суток, внешняя сеть).
Связи «панели → инвертор → АКБ», «дизель → бак», «щиток → нагрузки»
задаются позже правилами через входы control_*.
"""
from __future__ import annotations

from typing import Any, Optional

from ..environment import Environment, OUProcess, clamp, relax
from ..events import Severity
from .base import register_node_type
from .sandbox import ControlError, SandboxNode, control, parse_choice, parse_number

ON, OFF, FAULT = "ON", "OFF", "FAULT"


# ---------------------------------------------------------------------------
# Общие модели
# ---------------------------------------------------------------------------

def pv_output_w(env: Environment, rated_w: float, temp_coeff: float = 0.004) -> tuple[float, float]:
    """Мощность PV-массива в точке максимальной мощности и температура ячеек."""
    irradiance = env.outdoor.irradiance_w_m2
    cell_temp_c = env.outdoor.temperature_c + irradiance * 0.03
    power = rated_w * irradiance / 1000.0 * (1.0 - temp_coeff * (cell_temp_c - 25.0))
    return max(0.0, power), cell_temp_c


def dome_base_load_w(hour: float) -> float:
    """Типичное потребление купола от инвертора: ночь — дежурные нагрузки,
    день — мастерская, вечер — освещение."""
    if hour < 6 or hour >= 23:
        return 170.0
    if 18 <= hour < 23:
        return 420.0
    if 9 <= hour < 18:
        return 360.0
    return 250.0


class BatteryModel:
    """
    LiFePO4 8S (номинал 25.6 В). Используется и узлом battery, и инвертором
    (у инвертора — своё «видение» АКБ до появления правила синхронизации).
    """

    CELLS = 8
    INTERNAL_RESISTANCE_OHM = 0.03

    def __init__(self, capacity_ah: float, soc: float = 0.8, discharge_cutoff_soc: float = 0.2,
                 max_charge_c: float = 0.5, max_discharge_c: float = 1.0) -> None:
        self.nominal_capacity_ah = capacity_ah
        self.soc = soc
        self.discharge_cutoff_soc = discharge_cutoff_soc
        self.max_charge_c = max_charge_c
        self.max_discharge_c = max_discharge_c
        self.capacity_fade = 0.0  # доля потерянной ёмкости
        self.throughput_ah = 0.0

    @property
    def nominal_voltage(self) -> float:
        return 3.2 * self.CELLS

    @property
    def nominal_kwh(self) -> float:
        return self.nominal_capacity_ah * self.nominal_voltage / 1000.0

    @property
    def actual_capacity_ah(self) -> float:
        return self.nominal_capacity_ah * (1.0 - self.capacity_fade)

    def open_circuit_voltage(self) -> float:
        s = self.soc
        if s < 0.1:
            cell = 2.9 + 3.0 * s
        elif s < 0.9:
            cell = 3.2 + 0.1 * (s - 0.1) / 0.8
        else:
            cell = 3.3 + 1.0 * (s - 0.9)
        return cell * self.CELLS

    def step(self, requested_w: float, gdt: float) -> tuple[float, float, float]:
        """requested_w > 0 — заряд, < 0 — разряд. Возвращает (voltage, current, power)
        с учётом ограничений BMS (ток, отсечки по SOC)."""
        ocv = self.open_circuit_voltage()
        current = requested_w / ocv
        max_charge_a = self.max_charge_c * self.nominal_capacity_ah
        max_discharge_a = self.max_discharge_c * self.nominal_capacity_ah
        if self.soc >= 0.999:
            max_charge_a = 0.0
        elif self.soc > 0.95:
            max_charge_a *= (1.0 - self.soc) / 0.05  # CV-фаза: ток спадает
        if self.soc <= self.discharge_cutoff_soc:
            max_discharge_a = 0.0
        current = clamp(current, -max_discharge_a, max_charge_a)

        delta_ah = current * gdt / 3600.0
        self.soc = clamp(self.soc + delta_ah / self.actual_capacity_ah, 0.0, 1.0)
        self.throughput_ah += abs(delta_ah)
        # ~0.02 % ёмкости за эквивалентный полный цикл
        self.capacity_fade = min(0.4, self.throughput_ah / (2 * self.nominal_capacity_ah) * 0.0002)

        voltage = ocv + current * self.INTERNAL_RESISTANCE_OHM
        return voltage, current, voltage * current


# ---------------------------------------------------------------------------
# 1.1 Солнечные панели
# ---------------------------------------------------------------------------

@register_node_type("solar_panels")
class SolarPanels(SandboxNode):
    title = "Солнечные панели"
    system = "Энергетика"
    category = "power"

    def __init__(self, node_id: str, channels: int = 2, channel_rated_w: float = 600.0,
                 vmp_v: float = 72.0, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        if channels < 1:
            raise ValueError("channels должен быть >= 1")
        self.channels = channels
        self.channel_rated_w = channel_rated_w
        self.vmp_v = vmp_v
        self._soiling = [OUProcess(0.97, 0.01, 6 * 3600, self.rng) for _ in range(channels)]

    def initial_state(self) -> dict[str, Any]:
        return {
            "power_w": 0.0,
            "pv_voltage_v": 0.0,
            "pv_current_a": 0.0,
            "channels_count": self.channels,
            "mppt_channels_enabled": [True] * self.channels,
            "mppt_channel_power_w": [0.0] * self.channels,
            "mppt_1_enabled": True,
            "all_mppt_enabled": True,
            "active_mppt_count": self.channels,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        enabled = node["mppt_channels_enabled"]
        mpp_w, cell_t = pv_output_w(self.env, self.channel_rated_w)
        irradiance = self.env.outdoor.irradiance_w_m2

        channel_power = []
        for idx in range(self.channels):
            soiling = self._soiling[idx].step(gdt)
            p = mpp_w * soiling * (1.0 + self.noise(0.01)) if enabled[idx] else 0.0
            channel_power.append(round(max(0.0, p), 1))
        total = sum(channel_power)

        if irradiance < 3.0:
            voltage = max(0.0, self.noise(0.3))
        else:
            v_temp = 1.0 - 0.0035 * (cell_t - 25.0)
            v_light = 0.9 + 0.1 * min(1.0, irradiance / 200.0)
            voltage = self.vmp_v * v_temp * v_light
            if total <= 0.0:
                voltage *= 1.22  # нагрузки нет — напряжение холостого хода
            voltage += self.noise(0.2)
        current = total / voltage if voltage > 1.0 else 0.0

        return {
            "power_w": round(total, 1),
            "pv_voltage_v": round(voltage, 2),
            "pv_current_a": round(current, 2),
            "mppt_channel_power_w": channel_power,
        }

    # ---- управление ----

    def _set_channels(self, node: dict[str, Any], indices: list[int], value: bool) -> dict[str, Any]:
        enabled = list(node["mppt_channels_enabled"])
        for idx in indices:
            enabled[idx] = value
        return {
            "mppt_channels_enabled": enabled,
            "mppt_1_enabled": enabled[0],
            "all_mppt_enabled": all(enabled),
            "active_mppt_count": sum(enabled),
        }

    def _channel_index(self, value: Any) -> int:
        return int(parse_number(value, 1, self.channels, "Номер MPPT-канала")) - 1

    @control("enable_mppt_1")
    def _enable_mppt_1(self, node, value):
        return self._set_channels(node, [0], True)

    @control("disable_mppt_1")
    def _disable_mppt_1(self, node, value):
        return self._set_channels(node, [0], False)

    @control("enable_all_mppt")
    def _enable_all(self, node, value):
        return self._set_channels(node, list(range(self.channels)), True)

    @control("disable_all_mppt")
    def _disable_all(self, node, value):
        return self._set_channels(node, list(range(self.channels)), False)

    @control("enable_mppt")
    def _enable_channel(self, node, value):
        return self._set_channels(node, [self._channel_index(value)], True)

    @control("disable_mppt")
    def _disable_channel(self, node, value):
        return self._set_channels(node, [self._channel_index(value)], False)


# ---------------------------------------------------------------------------
# 1.2 Солнечный инвертор MUST PH1800 PRO
# ---------------------------------------------------------------------------

INVERTER_MODES = ("HYBRID", "GRID_ONLY", "BATTERY_ONLY")
INVERTER_ERRORS = {
    "E02": "OVER_TEMPERATURE",
    "E04": "BATTERY_UNDERVOLTAGE",
    "E07": "OVERLOAD",
}


@register_node_type("solar_inverter")
class SolarInverter(SandboxNode):
    """
    Гибридный инвертор. Сам распределяет потоки энергии между PV, АКБ,
    внешней сетью и нагрузкой в зависимости от режима:
        HYBRID       — PV → нагрузка, излишек в АКБ, дефицит из АКБ, затем из сети;
        GRID_ONLY    — нагрузка от сети, PV только заряжает АКБ (без сети — от АКБ);
        BATTERY_ONLY — автономно, сеть не используется.

    Входы для правил: control_load_w (нагрузка со щитка), control_pv_power_w
    (фактическая мощность панелей). None — используется собственная генерация.
    """
    title = "Солнечный инвертор MUST PH1800 PRO"
    system = "Энергетика"
    category = "power"

    def __init__(self, node_id: str, rated_w: float = 1800.0, pv_rated_w: float = 1200.0,
                 battery_capacity_ah: float = 100.0, pv_vmp_v: float = 72.0,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.rated_w = rated_w
        self.pv_rated_w = pv_rated_w
        self.pv_vmp_v = pv_vmp_v
        self.battery = BatteryModel(battery_capacity_ah)
        self._load_noise = OUProcess(0.0, 40.0, 900, self.rng)
        self._balancing_remaining_s = 0.0

    def initial_state(self) -> dict[str, Any]:
        return {
            "pv_voltage_v": 0.0,
            "pv_power_w": 0.0,
            "battery_voltage_v": round(self.battery.open_circuit_voltage(), 2),
            "battery_current_a": 0.0,
            "battery_power_w": 0.0,
            "battery_soc_pct": round(self.battery.soc * 100, 1),
            "battery_capacity_kwh": round(self.battery.nominal_kwh, 2),
            "load_power_w": 0.0,
            "output_voltage_v": 230.0,
            "grid_power_w": 0.0,
            "grid_state": "AVAILABLE",
            "grid_voltage_v": 230.0,
            "mode": "HYBRID",
            "mppt_enabled": True,
            "mppt_state": "IDLE",          # TRACKING | IDLE | OFF
            "inverter_temp_c": 30.0,
            "fan_mode": "AUTO",            # AUTO | ON | OFF
            "fan_state": OFF,
            "error": False,
            "error_code": None,
            "error_text": None,
            "balancing_state": "IDLE",     # IDLE | BALANCING | COMPLETED | CANCELLED
            "control_load_w": None,
            "control_pv_power_w": None,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        ext = self.env.external
        grid_ok = ext.grid_available
        mode = node["mode"]
        error_code = node["error_code"]

        # --- PV ---
        mpp_w, _ = pv_output_w(self.env, self.pv_rated_w)
        if node["control_pv_power_w"] is not None:
            mpp_w = float(node["control_pv_power_w"])
        irradiance = self.env.outdoor.irradiance_w_m2
        if not node["mppt_enabled"]:
            pv_w, mppt_state = 0.0, "OFF"
        elif mpp_w > 5.0:
            pv_w, mppt_state = mpp_w * (0.97 + self.noise(0.005)), "TRACKING"
        else:
            pv_w, mppt_state = 0.0, "IDLE"
        pv_v = 0.0 if irradiance < 3.0 else self.pv_vmp_v * (1.22 if pv_w == 0 else 1.0) + self.noise(0.3)

        # --- нагрузка ---
        if node["control_load_w"] is not None:
            load_w = float(node["control_load_w"])
        else:
            load_w = max(50.0, dome_base_load_w(self.hour) + self._load_noise.step(gdt))

        # --- ошибки, блокирующие выход ---
        if node["inverter_temp_c"] > 80.0:
            error_code = "E02"
        if load_w > self.rated_w * 1.1:
            error_code = "E07"
        output_blocked = error_code in {"E02", "E07"}
        if output_blocked:
            load_w = 0.0

        # --- распределение энергии ---
        grid_first = mode == "GRID_ONLY" and grid_ok
        pv_to_load = 0.0 if grid_first else min(pv_w, load_w)
        pv_surplus = pv_w - pv_to_load
        deficit = load_w - pv_to_load

        battery_request = pv_surplus
        grid_w = 0.0
        if deficit > 0:
            if grid_first:
                grid_w = deficit
            else:
                battery_request = -deficit  # здесь PV целиком ушёл в нагрузку, излишка нет

        batt_v, batt_a, batt_w = self.battery.step(battery_request, gdt)
        unserved = 0.0
        if battery_request < 0:
            # Сколько АКБ не смогла отдать (отсечка BMS по SOC или по току)
            missing = -battery_request - max(0.0, -batt_w)
            if missing > 1.0:
                if mode == "HYBRID" and grid_ok:
                    grid_w += missing
                else:
                    unserved = missing
        if unserved > 1.0 and error_code is None:
            error_code = "E04"
        elif error_code == "E04" and unserved <= 1.0 and self.battery.soc > self.battery.discharge_cutoff_soc + 0.05:
            error_code = None  # восстановление после разряда — автоматически

        output_v = 0.0 if (output_blocked or unserved > 1.0) else 230.0 + self.noise(1.0)
        served_load = 0.0 if output_v == 0.0 else load_w

        # --- тепловой режим и вентилятор ---
        fan_mode = node["fan_mode"]
        fan_on = node["fan_state"] == ON
        if fan_mode == "AUTO":
            fan_on = node["inverter_temp_c"] > 45.0 or (fan_on and node["inverter_temp_c"] > 38.0)
        else:
            fan_on = fan_mode == "ON"
        heat = (served_load + pv_w + abs(batt_w)) / self.rated_w * 38.0
        target_t = self.env.indoor.temperature_c + heat - (12.0 if fan_on else 0.0)
        temp = relax(node["inverter_temp_c"], target_t, 900, gdt) + self.noise(0.2)

        # --- балансировка ---
        balancing = node["balancing_state"]
        if balancing == "BALANCING":
            self._balancing_remaining_s -= gdt
            if self._balancing_remaining_s <= 0:
                balancing = "COMPLETED"
                self.emit("inverter.balancing_completed")

        if error_code and error_code != node["error_code"]:
            self.emit("inverter.error", Severity.CRITICAL, error_code=error_code,
                      error_text=INVERTER_ERRORS[error_code])

        return {
            "pv_voltage_v": round(max(0.0, pv_v), 2),
            "pv_power_w": round(pv_w, 1),
            "battery_voltage_v": round(batt_v, 2),
            "battery_current_a": round(batt_a, 2),
            "battery_power_w": round(batt_w, 1),
            "battery_soc_pct": round(self.battery.soc * 100, 1),
            "battery_capacity_kwh": round(self.battery.nominal_kwh, 2),
            "load_power_w": round(served_load, 1),
            "output_voltage_v": round(output_v, 1),
            "grid_power_w": round(grid_w, 1),
            "grid_state": "AVAILABLE" if grid_ok else "UNAVAILABLE",
            "grid_voltage_v": round(ext.grid_voltage_v, 1),
            "mppt_state": mppt_state,
            "inverter_temp_c": round(temp, 1),
            "fan_state": ON if fan_on else OFF,
            "error": error_code is not None,
            "error_code": error_code,
            "error_text": INVERTER_ERRORS.get(error_code) if error_code else None,
            "balancing_state": balancing,
        }

    # ---- управление ----

    @control("start_cell_balancing")
    def _start_balancing(self, node, value):
        if node["balancing_state"] == "BALANCING":
            raise ControlError("Балансировка уже выполняется")
        self._balancing_remaining_s = 45 * 60.0
        return {"balancing_state": "BALANCING"}

    @control("cancel_balancing")
    def _cancel_balancing(self, node, value):
        if node["balancing_state"] != "BALANCING":
            raise ControlError("Балансировка не выполняется")
        self._balancing_remaining_s = 0.0
        return {"balancing_state": "CANCELLED"}

    @control("set_mode_hybrid", "hybrid")
    def _mode_hybrid(self, node, value):
        return {"mode": "HYBRID"}

    @control("set_mode_grid_only", "grid_only")
    def _mode_grid_only(self, node, value):
        return {"mode": "GRID_ONLY"}

    @control("set_mode")
    def _set_mode(self, node, value):
        return {"mode": parse_choice(value, INVERTER_MODES, "Режим инвертора")}

    @control("enable_mppt")
    def _enable_mppt(self, node, value):
        return {"mppt_enabled": True}

    @control("disable_mppt")
    def _disable_mppt(self, node, value):
        return {"mppt_enabled": False, "mppt_state": "OFF"}

    @control("reset_error")
    def _reset_error(self, node, value):
        return {"error": False, "error_code": None, "error_text": None}

    @control("set_fan")
    def _set_fan(self, node, value):
        return {"fan_mode": parse_choice(value, ("AUTO", "ON", "OFF"), "Режим вентилятора")}


# ---------------------------------------------------------------------------
# 1.3 Аккумуляторная батарея
# ---------------------------------------------------------------------------

@register_node_type("battery")
class Battery(SandboxNode):
    """
    Данные BMS. Без правил АКБ сама оценивает поток энергии (PV минус типовая
    нагрузка купола). Вход для правил: control_power_w (+заряд / −разряд, Вт) —
    например, из battery_power_w инвертора.
    """
    title = "Аккумуляторная батарея (АКБ)"
    system = "Энергетика"
    category = "power"

    def __init__(self, node_id: str, capacity_ah: float = 100.0, pv_rated_w: float = 1200.0,
                 initial_soc: float = 0.8, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.model = BatteryModel(capacity_ah, soc=initial_soc)
        self.pv_rated_w = pv_rated_w
        self._load_noise = OUProcess(0.0, 40.0, 900, self.rng)

    def initial_state(self) -> dict[str, Any]:
        m = self.model
        return {
            "soc_pct": round(m.soc * 100, 1),
            "voltage_v": round(m.open_circuit_voltage(), 2),
            "current_a": 0.0,
            "power_w": 0.0,
            "nominal_capacity_ah": m.nominal_capacity_ah,
            "nominal_capacity_kwh": round(m.nominal_kwh, 2),
            "estimated_capacity_kwh": round(m.nominal_kwh, 3),
            "state": "IDLE",               # CHARGING | DISCHARGING | IDLE
            "balancing_state": "IDLE",     # IDLE | BALANCING
            "cell_voltage_delta_mv": 8.0,
            "control_power_w": None,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        m = self.model
        if node["control_power_w"] is not None:
            request = float(node["control_power_w"])
        else:
            pv, _ = pv_output_w(self.env, self.pv_rated_w)
            request = pv * 0.95 - max(50.0, dome_base_load_w(self.hour) + self._load_noise.step(gdt))

        voltage, current, power = m.step(request, gdt)
        state = "CHARGING" if current > 0.2 else ("DISCHARGING" if current < -0.2 else "IDLE")

        # Пассивная балансировка BMS в конце заряда — разбаланс ячеек уменьшается
        delta = node["cell_voltage_delta_mv"]
        balancing = state == "CHARGING" and m.soc > 0.95
        delta = relax(delta, 3.0, 1800, gdt) if balancing else delta + abs(current) * gdt / 3600.0 * 0.05
        delta = clamp(delta + self.noise(0.3), 1.0, 150.0)

        was_low = node["soc_pct"] < 25.0
        if m.soc * 100 < 25.0 and not was_low:
            self.emit("battery.low", Severity.WARNING, soc_pct=round(m.soc * 100, 1))

        return {
            "soc_pct": round(m.soc * 100, 1),
            "voltage_v": round(voltage + self.noise(0.01), 2),
            "current_a": round(current, 2),
            "power_w": round(power, 1),
            "estimated_capacity_kwh": round(m.actual_capacity_ah * m.nominal_voltage / 1000.0, 3),
            "state": state,
            "balancing_state": "BALANCING" if balancing else "IDLE",
            "cell_voltage_delta_mv": round(delta, 1),
        }


# ---------------------------------------------------------------------------
# 1.4 Ветрогенератор
# ---------------------------------------------------------------------------

@register_node_type("wind_turbine")
class WindTurbine(SandboxNode):
    title = "Ветрогенератор"
    system = "Энергетика"
    category = "power"

    def __init__(self, node_id: str, rated_w: float = 600.0, cut_in_ms: float = 3.0,
                 rated_speed_ms: float = 12.0, cut_out_ms: float = 22.0, max_rpm: float = 800.0,
                 fault_rate_per_hour: float = 0.002, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.rated_w = rated_w
        self.cut_in_ms = cut_in_ms
        self.rated_speed_ms = rated_speed_ms
        self.cut_out_ms = cut_out_ms
        self.max_rpm = max_rpm
        self.fault_rate_per_hour = fault_rate_per_hour

    def initial_state(self) -> dict[str, Any]:
        return {
            "wind_speed_ms": 0.0,
            "rpm": 0.0,
            "power_w": 0.0,
            "state": ON,
            "available": True,
            "fault_code": None,   # OVERSPEED | YAW_ERROR
        }

    def _power_curve(self, wind: float) -> float:
        if wind < self.cut_in_ms or wind >= self.cut_out_ms:
            return 0.0
        if wind >= self.rated_speed_ms:
            return self.rated_w
        x = (wind - self.cut_in_ms) / (self.rated_speed_ms - self.cut_in_ms)
        return self.rated_w * x ** 3

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        wind = max(0.0, self.env.outdoor.wind_speed_ms * (1.0 + self.noise(0.05)))
        state, fault = node["state"], node["fault_code"]

        if state == ON and wind >= self.cut_out_ms:
            state, fault = FAULT, "OVERSPEED"
            self.emit("wind_turbine.fault", Severity.WARNING, fault_code=fault, wind_speed_ms=round(wind, 1))
        elif state == ON and self.happens(self.fault_rate_per_hour, gdt):
            state, fault = FAULT, "YAW_ERROR"
            self.emit("wind_turbine.fault", Severity.WARNING, fault_code=fault)
        elif state == FAULT and fault == "OVERSPEED" and wind < 0.8 * self.cut_out_ms:
            state, fault = ON, None  # штормовая защита снимается автоматически

        if state == ON:
            power = self._power_curve(wind) * (1.0 + self.noise(0.03))
            target_rpm = min(self.max_rpm, self.max_rpm * wind / self.rated_speed_ms)
        else:
            power, target_rpm = 0.0, 0.0  # тормоз
        rpm = relax(node["rpm"], target_rpm, 30, gdt)

        return {
            "wind_speed_ms": round(wind, 2),
            "rpm": round(max(0.0, rpm + self.noise(3.0) if rpm > 5 else 0.0), 0),
            "power_w": round(max(0.0, power), 1),
            "state": state,
            "available": state != FAULT,
            "fault_code": fault,
        }

    @control("turn_on", "on", "start", "enable")
    def _turn_on(self, node, value):
        if node["state"] == FAULT and node["fault_code"] == "OVERSPEED":
            raise ControlError("Штормовая защита активна: включение невозможно до снижения ветра")
        return {"state": ON, "available": True, "fault_code": None}

    @control("turn_off", "off", "stop", "disable")
    def _turn_off(self, node, value):
        return {"state": OFF, "available": True, "fault_code": None}


# ---------------------------------------------------------------------------
# 1.5 Дизельный генератор
# ---------------------------------------------------------------------------

@register_node_type("dizel")
class DieselGenerator(SandboxNode):
    """
    Входы для правил: control_load_w (нагрузка на генератор), control_fuel_available
    (False — топливо закончилось, генератор глохнет с FAULT).
    """
    title = "Дизельный генератор"
    system = "Энергетика"
    category = "power"

    def __init__(self, node_id: str, rated_w: float = 3000.0, start_failure_prob: float = 0.03,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.rated_w = rated_w
        self.start_failure_prob = start_failure_prob
        self._load = OUProcess(0.55, 0.1, 1200, self.rng)

    def initial_state(self) -> dict[str, Any]:
        return {
            "power_w": 0.0,
            "state": OFF,
            "fault_code": None,       # START_FAILURE | NO_FUEL | OVERHEAT
            "engine_temp_c": 20.0,
            "runtime_h": 0.0,
            "control_load_w": None,
            "control_fuel_available": True,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        state, fault = node["state"], node["fault_code"]
        ambient = self.env.outdoor.temperature_c

        if state == ON and not node["control_fuel_available"]:
            state, fault = FAULT, "NO_FUEL"
            self.emit("dizel.fault", Severity.CRITICAL, fault_code=fault)

        if state == ON:
            if node["control_load_w"] is not None:
                power = min(float(node["control_load_w"]), self.rated_w)
            else:
                power = self.rated_w * clamp(self._load.step(gdt), 0.15, 1.0)
            temp_target = 85.0 + 8.0 * power / self.rated_w
            if node["engine_temp_c"] < 60.0:
                power *= 0.6  # прогрев: генератор не выдаёт полную мощность
        else:
            power, temp_target = 0.0, ambient
        temp = relax(node["engine_temp_c"], temp_target, 600, gdt) + self.noise(0.3)

        if state == ON and temp > 105.0:
            state, fault, power = FAULT, "OVERHEAT", 0.0
            self.emit("dizel.fault", Severity.CRITICAL, fault_code=fault)

        return {
            "power_w": round(power, 1),
            "state": state,
            "fault_code": fault,
            "engine_temp_c": round(temp, 1),
            "runtime_h": round(node["runtime_h"] + (gdt / 3600.0 if state == ON else 0.0), 3),
        }

    @control("turn_on", "on", "start", "enable")
    def _turn_on(self, node, value):
        if node["state"] == ON:
            return None
        if not node["control_fuel_available"]:
            self.emit("dizel.fault", Severity.CRITICAL, fault_code="NO_FUEL")
            return {"state": FAULT, "fault_code": "NO_FUEL"}
        if self.rng.random() < self.start_failure_prob:
            self.emit("dizel.fault", Severity.WARNING, fault_code="START_FAILURE")
            return {"state": FAULT, "fault_code": "START_FAILURE"}
        return {"state": ON, "fault_code": None}

    @control("turn_off", "off", "stop", "disable")
    def _turn_off(self, node, value):
        return {"state": OFF, "fault_code": None, "power_w": 0.0}


# ---------------------------------------------------------------------------
# 1.6 Топливный бак
# ---------------------------------------------------------------------------

@register_node_type("fuel_tank")
class FuelTank(SandboxNode):
    """
    Вход для правил: control_consumption_l_h (расход дизеля). Без правил расход 0.
    Вода в топливе медленно накапливается от конденсата (чем больше воздуха в баке
    и выше влажность — тем быстрее).
    """
    title = "Топливный бак дизель-генератора"
    system = "Энергетика"
    category = "power"

    def __init__(self, node_id: str, capacity_l: float = 200.0, initial_level: float = 0.75,
                 reserve_l: float = 150.0, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.capacity_l = capacity_l
        self.initial_level = initial_level
        self.reserve_l = reserve_l

    def initial_state(self) -> dict[str, Any]:
        volume = self.capacity_l * self.initial_level
        return {
            "capacity_l": self.capacity_l,
            "volume_l": round(volume, 2),
            "level_pct": round(self.initial_level * 100, 1),
            "consumption_l_h": 0.0,
            "water_content_ppm": 120.0,
            "contamination_pct": 0.3,
            "fuel_quality": "GOOD",        # GOOD | WATER | CONTAMINATED
            "fuel_temp_c": 12.0,
            "state": "NORMAL",             # NORMAL | LOW | CRITICAL | CONTAMINATED
            "reserve_l": self.reserve_l,
            "control_consumption_l_h": None,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        rate = float(node["control_consumption_l_h"] or 0.0)
        volume = max(0.0, node["volume_l"] - rate * gdt / 3600.0)
        level = volume / self.capacity_l

        humidity = self.env.outdoor.humidity_pct / 100.0
        water = node["water_content_ppm"] + (1.0 - level) * humidity * 6.0 * gdt / 3600.0
        contamination = node["contamination_pct"] + 0.01 * gdt / 86400.0
        temp = relax(node["fuel_temp_c"], self.env.outdoor.temperature_c, 8 * 3600, gdt)

        contaminated = water > 500.0 or contamination > 5.0
        quality = "CONTAMINATED" if contamination > 5.0 else ("WATER" if water > 500.0 else "GOOD")
        if contaminated:
            state = "CONTAMINATED"
        elif level < 0.10:
            state = "CRITICAL"
        elif level < 0.25:
            state = "LOW"
        else:
            state = "NORMAL"
        if state != node["state"] and state != "NORMAL":
            self.emit("fuel_tank.state", Severity.CRITICAL if state == "CRITICAL" else Severity.WARNING,
                      state=state, level_pct=round(level * 100, 1))

        return {
            "volume_l": round(volume, 2),
            "level_pct": round(level * 100 + self.noise(0.1), 1),
            "consumption_l_h": round(rate, 2),
            "water_content_ppm": round(water, 3),        # накопители: без грубого округления,
            "contamination_pct": round(contamination, 6),  # иначе прирост за тик теряется
            "fuel_quality": quality,
            "fuel_temp_c": round(temp + self.noise(0.1), 1),
            "state": state,
        }

    @control("transfer_fuel")
    def _transfer(self, node, value):
        """Перекачка из резервной ёмкости. value — литры (по умолчанию — до полного)."""
        free = self.capacity_l - node["volume_l"]
        wanted = free if value is None else parse_number(value, 0.0, self.capacity_l, "Объём перекачки, л")
        amount = min(wanted, free, node["reserve_l"])
        if amount <= 0:
            raise ControlError("Перекачка невозможна: бак полон или резерв пуст")
        volume = node["volume_l"] + amount
        return {"volume_l": round(volume, 2), "reserve_l": round(node["reserve_l"] - amount, 2),
                "level_pct": round(volume / self.capacity_l * 100, 1)}


# ---------------------------------------------------------------------------
# 2.1 Умный щиток на 8 линий
# ---------------------------------------------------------------------------

def _load_production(hour: float) -> float:
    return 380.0 if 9 <= hour < 19 else 60.0


def _load_cnc(hour: float) -> float:
    return 180.0 if 9 <= hour < 19 else 4.0


def _load_printer(hour: float) -> float:
    return 140.0 if 8 <= hour < 22 else 5.0


def _load_lighting(hour: float) -> float:
    if 6 <= hour < 9 or 18 <= hour < 23:
        return 190.0
    return 70.0 if 9 <= hour < 18 else 25.0


def _load_emergency(hour: float) -> float:
    return 12.0  # подзаряд встроенных аккумуляторов светильников


def _load_fume(hour: float) -> float:
    return 95.0 if 9 <= hour < 19 else 0.0


def _load_none(hour: float) -> float:
    return 0.0


# (номер, назначение, номинал автомата, А, профиль нагрузки, Вт)
PANEL_LINES = [
    (1, "Производственный отдел", 16.0, _load_production),
    (2, "ЧПУ-фрезер", 10.0, _load_cnc),
    (3, "3D-принтер", 10.0, _load_printer),
    (4, "Обычное освещение", 10.0, _load_lighting),
    (5, "Аварийное освещение", 6.0, _load_emergency),
    (6, "Вытяжка FabLab", 10.0, _load_fume),
    (7, "Резервная линия", 16.0, _load_none),
    (8, "Резервная линия / фантомное потребление", 16.0, _load_none),
]

LINE_ON, LINE_OFF, LINE_TRIPPED, LINE_RCD_TRIP = "ON", "OFF", "TRIPPED", "RCD_TRIP"


@register_node_type("smart_panel")
class SmartPanel(SandboxNode):
    """
    Щиток на 8 линий. Телеметрия линий — список `lines` (индекс 0 = линия 1).

    Входы для правил и кризисов:
        control_line_loads_w: {"2": 250.0, ...} — фактическая нагрузка линии
            (например, от ЧПУ или принтера); линии без записи используют профиль;
        control_phantom_load_w: добавочная «фантомная» нагрузка на линию 8.
    """
    title = "Умный щиток на 8 линий"
    system = "Электроснабжение"
    category = "power"

    POWER_FACTOR = 0.95

    def __init__(self, node_id: str, nuisance_trip_rate_per_hour: float = 0.001,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.nuisance_trip_rate_per_hour = nuisance_trip_rate_per_hour
        self._noise = [OUProcess(1.0, 0.12, 600, self.rng) for _ in PANEL_LINES]

    def initial_state(self) -> dict[str, Any]:
        lines = []
        for number, name, rated_a, _ in PANEL_LINES:
            lines.append({
                "line": number,
                "name": name,
                "rated_current_a": rated_a,
                "state": LINE_ON,
                "voltage_v": 230.0,
                "current_a": 0.0,
                "power_w": 0.0,
                "protection_state": "ARMED",   # ARMED | TRIPPED | RCD_TRIPPED
                "alarm": False,
                "alarm_code": None,            # OVERCURRENT | RCD_LEAKAGE | EMULATED | ...
            })
        return {
            "lines": lines,
            "bus_voltage_v": 230.0,
            "total_power_w": 0.0,
            "total_current_a": 0.0,
            "lines_on_count": len(lines),
            "control_line_loads_w": {},
            "control_phantom_load_w": 0.0,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        overrides = node.get("control_line_loads_w") or {}
        phantom = float(node.get("control_phantom_load_w") or 0.0)
        hour = self.hour

        demands = []
        for idx, (number, _, _, profile) in enumerate(PANEL_LINES):
            if str(number) in overrides:
                demand = float(overrides[str(number)])
            else:
                demand = profile(hour) * clamp(self._noise[idx].step(gdt), 0.3, 2.0)
            if number == 8:
                demand += phantom
            demands.append(max(0.0, demand))

        live_current = sum(d / 230.0 for d, l in zip(demands, node["lines"]) if l["state"] == LINE_ON)
        bus_v = 230.0 + self.noise(1.2) - 0.08 * live_current

        lines, total_p, total_i = [], 0.0, 0.0
        for line, demand in zip(node["lines"], demands):
            line = dict(line)
            if line["state"] == LINE_ON:
                current = demand / (bus_v * self.POWER_FACTOR)
                if current > line["rated_current_a"]:
                    line.update(state=LINE_TRIPPED, protection_state="TRIPPED",
                                alarm=True, alarm_code="OVERCURRENT")
                    self.emit("smart_panel.line_tripped", Severity.CRITICAL, line=line["line"],
                              reason="OVERCURRENT", current_a=round(current, 2))
                elif self.happens(self.nuisance_trip_rate_per_hour, gdt):
                    line.update(state=LINE_RCD_TRIP, protection_state="RCD_TRIPPED",
                                alarm=True, alarm_code="RCD_LEAKAGE")
                    self.emit("smart_panel.line_tripped", Severity.WARNING, line=line["line"],
                              reason="RCD_LEAKAGE")
            if line["state"] == LINE_ON:
                current = demand / (bus_v * self.POWER_FACTOR)
                line.update(voltage_v=round(bus_v, 1), current_a=round(current, 3), power_w=round(demand, 1))
                total_p += demand
                total_i += current
            else:
                line.update(voltage_v=0.0, current_a=0.0, power_w=0.0)
            lines.append(line)

        return {
            "lines": lines,
            "bus_voltage_v": round(bus_v, 1),
            "total_power_w": round(total_p, 1),
            "total_current_a": round(total_i, 2),
            "lines_on_count": sum(1 for l in lines if l["state"] == LINE_ON),
        }

    # ---- управление: value = номер линии (1..8) или {"line": n, ...} ----

    @staticmethod
    def _line_number(value: Any) -> int:
        if isinstance(value, dict):
            value = value.get("line")
        return int(parse_number(value, 1, len(PANEL_LINES), "Номер линии"))

    def _with_line(self, node: dict[str, Any], number: int, **changes: Any) -> dict[str, Any]:
        lines = [dict(l) for l in node["lines"]]
        lines[number - 1].update(changes)
        return {"lines": lines, "lines_on_count": sum(1 for l in lines if l["state"] == LINE_ON)}

    @control("line_on", "enable_line")
    def _line_on(self, node, value):
        number = self._line_number(value)
        line = node["lines"][number - 1]
        if line["protection_state"] != "ARMED":
            raise ControlError(f"Линия {number}: сработала защита, сначала выполните reset_protection")
        return self._with_line(node, number, state=LINE_ON)

    @control("line_off", "disable_line")
    def _line_off(self, node, value):
        number = self._line_number(value)
        line = node["lines"][number - 1]
        if line["state"] in (LINE_TRIPPED, LINE_RCD_TRIP):
            return None  # уже обесточена защитой
        return self._with_line(node, number, state=LINE_OFF)

    @control("reset_line_error")
    def _reset_error(self, node, value):
        return self._with_line(node, self._line_number(value), alarm=False, alarm_code=None)

    @control("reset_protection")
    def _reset_protection(self, node, value):
        """Взвод защиты: линия возвращается в ON (как повторное включение автомата)."""
        number = self._line_number(value)
        line = node["lines"][number - 1]
        if line["protection_state"] == "ARMED":
            raise ControlError(f"Линия {number}: защита не срабатывала")
        return self._with_line(node, number, state=LINE_ON, protection_state="ARMED")

    @control("emulate_protection_trip")
    def _emulate_trip(self, node, value):
        """value: номер линии или {"line": n, "type": "TRIPPED" | "RCD_TRIP"}."""
        number = self._line_number(value)
        trip = value.get("type", LINE_TRIPPED) if isinstance(value, dict) else LINE_TRIPPED
        trip = parse_choice(trip, (LINE_TRIPPED, LINE_RCD_TRIP), "Тип срабатывания")
        self.emit("smart_panel.line_tripped", Severity.WARNING, line=number, reason="EMULATED", type=trip)
        return self._with_line(node, number, state=trip, alarm=True, alarm_code="EMULATED",
                               protection_state="TRIPPED" if trip == LINE_TRIPPED else "RCD_TRIPPED")

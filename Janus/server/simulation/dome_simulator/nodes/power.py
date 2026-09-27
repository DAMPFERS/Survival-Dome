# dome_simulator/nodes/power.py
"""
Энергетика и электроснабжение (разделы 1–2 реестра dome_sandbox_nodes.md):
solar_panels, solar_inverter, battery, wind_turbine, dizel, fuel_tank, smart_panel.

Связи «панели → инвертор → АКБ → щиток → нагрузки», «дизель ↔ бак» задают
правила зависимостей (rules.py) через входы control_*. Без правил каждый узел
генерирует данные сам из общего окружения — так узлы тестируются по отдельности.

Кризисные входы (dome_crises.md, доработки Д8, Д9, Д11, Д15) — тоже control_*:
узел моделирует последствия, а собственные команды узла (reset_error и т.п.)
снимают сбой там, где это делает реальное устройство.
"""
from __future__ import annotations

from typing import Any, Optional

from ..environment import Environment, OUProcess, clamp, relax
from ..events import Severity
from ..store import StateStore
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
    """Типичное потребление купола от инвертора (без правил зависимостей):
    ночь — дежурные нагрузки, день — мастерская, вечер — освещение."""
    if hour < 6 or hour >= 23:
        return 170.0
    if 18 <= hour < 23:
        return 420.0
    if 9 <= hour < 18:
        return 360.0
    return 250.0


class BatteryModel:
    """
    LiFePO4 8S (номинал 25.6 В). Модель живёт в инверторе (он же BMS-мост);
    узел battery_01 при подключённых правилах зеркалирует её состояние.
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

    def step(self, requested_w: float, gdt: float, max_discharge_a: Optional[float] = None,
             charge_allowed: bool = True) -> tuple[float, float, float]:
        """requested_w > 0 — заряд, < 0 — разряд. Возвращает (voltage, current, power)
        с учётом ограничений BMS (ток, отсечки по SOC, запрет заряда)."""
        ocv = self.open_circuit_voltage()
        current = requested_w / ocv
        max_charge_a = self.max_charge_c * self.nominal_capacity_ah if charge_allowed else 0.0
        if max_discharge_a is None:
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
    """
    Два MPPT-канала. Загрязнение (Д11): пыль в воздухе оседает на панелях —
    при dust_level ≥ 0.8 потери растут ~2 %/мин (пыльная буря, К2); дождь частично
    смывает. Включение MPPT от настоящей пыли не помогает — нужна очистка
    (clean_panels, физическое действие).
    """
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
            "cell_temp_c": 20.0,
            "soiling_loss_pct": 0.0,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        enabled = node["mppt_channels_enabled"]
        mpp_w, cell_t = pv_output_w(self.env, self.channel_rated_w)
        irradiance = self.env.outdoor.irradiance_w_m2
        o = self.env.outdoor

        # Пыль: оседает при сильной запылённости, смывается дождём
        loss = node.get("soiling_loss_pct", 0.0)
        loss += max(0.0, o.dust_level - 0.5) * 4.0 * gdt / 60.0
        loss -= min(o.precipitation_mm_h, 5.0) * 2.0 * gdt / 60.0
        loss = clamp(loss, 0.0, 90.0)
        dust_k = 1.0 - loss / 100.0

        channel_power = []
        for idx in range(self.channels):
            soiling = self._soiling[idx].step(gdt)
            p = mpp_w * soiling * dust_k * (1.0 + self.noise(0.01)) if enabled[idx] else 0.0
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
            "cell_temp_c": round(cell_t, 1),
            "soiling_loss_pct": round(loss, 3),
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

    @control("clean_panels", physical=True)
    def _clean(self, node, value):
        """Очистка панелей от пыли (выполняет Оператор/персонал)."""
        return {"soiling_loss_pct": 0.0}


# ---------------------------------------------------------------------------
# 1.2 Солнечный инвертор MUST PH1800 PRO
# ---------------------------------------------------------------------------

INVERTER_MODES = ("HYBRID", "GRID_ONLY", "BATTERY_ONLY")
INVERTER_ERRORS = {
    "E02": "OVER_TEMPERATURE",
    "E04": "BATTERY_UNDERVOLTAGE",
    "E07": "OVERLOAD",
    "E09": "ISOLATION_FAULT",
}
FAN_FAULT_STOPPED, FAN_FAULT_SENSOR = "STOPPED", "SENSOR"


@register_node_type("solar_inverter")
class SolarInverter(SandboxNode):
    """
    Гибридный инвертор. Сам распределяет потоки энергии между PV, АКБ,
    внешней сетью (или дизелем на AC-входе) и нагрузкой:
        HYBRID       — PV → нагрузка, излишек в АКБ, дефицит из АКБ, затем с AC-входа;
        GRID_ONLY    — нагрузка от AC-входа, PV только заряжает АКБ; при пропадании
                       AC выход отключается на 10–20 с до перехода на АКБ (Д9);
        BATTERY_ONLY — автономно, AC-вход не используется.

    Входы правил: control_pv_power_w (мощность панелей), control_wind_power_w,
    control_load_w (щиток + службы купола), control_generator_w (дизель на AC-входе),
    control_charge_allowed (BMS разрешает заряд).

    Кризисные входы (Д8): control_isolation_fault (E09), control_fan_fault
    ("STOPPED" — вентилятор встал, "SENSOR" — ложный отказ датчика оборотов),
    control_fan_wear (износ подшипника → рост fan_current_a), control_grid_sense_fault
    (ложная нестабильность сети), control_pv_sense_k (занижение измерения PV),
    control_output_limit_pct (сниженная перегрузочная способность).

    Ошибки: E02 — перегрев (выход отключён до остывания ниже 65 °C), E04 — АКБ разряжена
    (выход отключён), E07 — перегрузка (выход отключён на 30 с, затем автоперезапуск),
    E09 — изоляция PV (вход PV заблокирован до reset_error).
    """
    title = "Солнечный инвертор MUST PH1800 PRO"
    system = "Энергетика"
    category = "power"

    E07_RESTART_S = 30.0
    OVERLOAD_GRACE_S = 30.0
    E09_REARM_S = 120.0
    DERATE_START_C, DERATE_END_C = 65.0, 80.0
    E02_RECOVER_C = 65.0

    def __init__(self, node_id: str, rated_w: float = 1800.0, pv_rated_w: float = 1200.0,
                 battery_capacity_ah: float = 100.0, pv_vmp_v: float = 72.0, initial_soc: float = 0.8,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.rated_w = rated_w
        self.pv_rated_w = pv_rated_w
        self.pv_vmp_v = pv_vmp_v
        self.battery = BatteryModel(battery_capacity_ah, soc=initial_soc)
        self._load_noise = OUProcess(0.0, 40.0, 900, self.rng)
        self._balancing_remaining_s = 0.0
        self._e07_remaining_s = 0.0
        self._overload_s = 0.0
        self._transfer_remaining_s = 0.0
        self._ac_was_available = True
        self._e09_rearm_s = 0.0
        self._deficit_s = 0.0

    def set_soc(self, soc_pct: float) -> None:
        """Установка истинного SOC (начальные условия сценария)."""
        self.battery.soc = clamp(soc_pct / 100.0, 0.0, 1.0)

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
            "output_limit_pct": 100.0,
            "grid_power_w": 0.0,
            "generator_power_w": 0.0,
            "ac_source": None,             # GRID | GENERATOR | None
            "grid_state": "AVAILABLE",     # AVAILABLE | UNSTABLE | UNAVAILABLE
            "grid_voltage_v": 230.0,
            "mode": "HYBRID",
            "mppt_enabled": True,
            "mppt_state": "IDLE",          # TRACKING | LIMITED | IDLE | OFF
            "battery_charge_blocked": False,
            "power_deficit_w": 0.0,
            "inverter_temp_c": 35.0,
            "fan_mode": "AUTO",            # AUTO | ON | OFF
            "fan_state": OFF,              # ON | OFF | FAULT
            "fan_current_a": 0.0,
            "error": False,
            "error_code": None,
            "error_text": None,
            "balancing_state": "IDLE",     # IDLE | BALANCING | COMPLETED | CANCELLED
            "control_load_w": None,
            "control_pv_power_w": None,
            "control_wind_power_w": None,
            "control_generator_w": None,
            "control_charge_allowed": None,
            "control_isolation_fault": False,
            "control_fan_fault": None,
            "control_fan_wear": 0.0,
            "control_grid_sense_fault": False,
            "control_pv_sense_k": None,
            "control_output_limit_pct": None,
        }

    # ---- вспомогательное ----

    def _output_limit_pct(self, node: dict[str, Any]) -> float:
        limit = 100.0
        temp = node["inverter_temp_c"]
        if temp > self.DERATE_START_C:  # дерейтинг по температуре: 65 °C → 100 %, 80 °C → 50 %
            k = clamp((temp - self.DERATE_START_C) / (self.DERATE_END_C - self.DERATE_START_C), 0.0, 1.0)
            limit = min(limit, 100.0 - 50.0 * k)
        if node["control_fan_fault"] == FAN_FAULT_SENSOR:
            limit = min(limit, 70.0)   # инвертор «считает» вентилятор остановившимся
        if node["control_output_limit_pct"] is not None:
            limit = min(limit, float(node["control_output_limit_pct"]))
        return limit

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        ext = self.env.external
        mode = node["mode"]
        error_code = node["error_code"]
        u: dict[str, Any] = {}

        # --- AC-вход: сеть или дизель ---
        grid_sense_fault = bool(node["control_grid_sense_fault"])
        grid_ok = ext.grid_available and not grid_sense_fault
        generator_w = float(node["control_generator_w"] or 0.0)
        if ext.grid_available and (ext.grid_unstable or grid_sense_fault):
            grid_state = "UNSTABLE"
        else:
            grid_state = "AVAILABLE" if ext.grid_available else "UNAVAILABLE"
        if grid_ok:
            ac_source, ac_capacity = "GRID", 1e9
        elif generator_w > 0:
            ac_source, ac_capacity = "GENERATOR", generator_w
        else:
            ac_source, ac_capacity = None, 0.0
        ac_ok = ac_source is not None

        # --- E09: изоляция PV ---
        if node["control_isolation_fault"]:
            if error_code is None and self._e09_rearm_s <= 0:
                error_code = "E09"
            self._e09_rearm_s = max(0.0, self._e09_rearm_s - gdt)
        else:
            self._e09_rearm_s = 0.0

        # --- PV (панели + ветрогенератор на DC-входе) ---
        if node["control_pv_power_w"] is not None:
            mpp_w = float(node["control_pv_power_w"])
        else:
            mpp_w, _ = pv_output_w(self.env, self.pv_rated_w)
        mpp_w += float(node["control_wind_power_w"] or 0.0)
        irradiance = self.env.outdoor.irradiance_w_m2
        sense_k = node["control_pv_sense_k"]
        tracking_k = 0.97
        if sense_k is not None and sense_k < 0.95:
            tracking_k = 0.97 * clamp(1.0 - (1.0 - sense_k) * 1.33, 0.2, 1.0)  # MPPT ушёл из точки
        if not node["mppt_enabled"]:
            pv_avail, mppt_state = 0.0, "OFF"
        elif error_code == "E09":
            pv_avail, mppt_state = 0.0, "OFF"
        elif mpp_w > 5.0:
            pv_avail, mppt_state = mpp_w * (tracking_k + self.noise(0.005)), "TRACKING"
        else:
            pv_avail, mppt_state = 0.0, "IDLE"

        # --- нагрузка ---
        if node["control_load_w"] is not None:
            load_w = float(node["control_load_w"])
        else:
            load_w = max(50.0, dome_base_load_w(self.hour) + self._load_noise.step(gdt))

        # --- ограничение выхода и перегрузка ---
        limit_pct = self._output_limit_pct(node)
        limit_w = self.rated_w * limit_pct / 100.0
        if self._e07_remaining_s > 0:
            self._e07_remaining_s -= gdt
            if self._e07_remaining_s <= 0 and error_code == "E07":
                error_code = None  # автоперезапуск после перегрузки
                self.emit("inverter.restarted", Severity.INFO)
        elif error_code not in ("E02", "E04") and load_w > 0:
            if load_w > limit_w * 1.1:
                self._overload_s = self.OVERLOAD_GRACE_S
            elif load_w > limit_w:
                self._overload_s += gdt
            else:
                self._overload_s = 0.0
            if self._overload_s >= self.OVERLOAD_GRACE_S:
                error_code, self._overload_s = "E07", 0.0
                self._e07_remaining_s = self.E07_RESTART_S

        # --- перегрев ---
        temp_now = node["inverter_temp_c"]
        if temp_now > 80.0 and error_code != "E02":
            error_code = "E02"
        elif error_code == "E02" and temp_now < self.E02_RECOVER_C:
            error_code = None
            self.emit("inverter.restarted", Severity.INFO, reason="cooled_down")

        # --- переход GRID_ONLY → АКБ при пропадании AC (Д9) ---
        if mode == "GRID_ONLY" and self._ac_was_available and not ac_ok:
            self._transfer_remaining_s = self.rng.uniform(10.0, 20.0)
            self.emit("inverter.transfer", Severity.WARNING, reason="ac_lost", gap_s=round(self._transfer_remaining_s, 1))
        self._ac_was_available = ac_ok
        transferring = self._transfer_remaining_s > 0
        if transferring:
            self._transfer_remaining_s -= gdt

        output_blocked = error_code in ("E02", "E04", "E07") or transferring
        if output_blocked:
            load_w = 0.0

        # --- распределение энергии ---
        balancing = node["balancing_state"]
        bms_allows = node["control_charge_allowed"] is not False
        charge_allowed = bms_allows and balancing != "BALANCING"
        discharge_limit_a = 0.1 * self.battery.nominal_capacity_ah if grid_sense_fault else None

        grid_first = mode == "GRID_ONLY" and ac_ok
        use_ac = mode != "BATTERY_ONLY" and ac_ok
        generator_feeds = use_ac and ac_source == "GENERATOR"
        pv_to_load = 0.0 if grid_first else min(pv_avail, load_w)
        pv_surplus = pv_avail - pv_to_load
        deficit = load_w - pv_to_load

        # AC-вход берёт нагрузку первым в GRID_ONLY, а запущенный дизель — в любом режиме
        ac_w = 0.0
        if deficit > 0 and (grid_first or generator_feeds):
            ac_w = min(deficit, ac_capacity)
            deficit -= ac_w
        charge_offer = pv_surplus
        if generator_feeds and self.battery.soc < 0.95:
            charge_offer += max(0.0, ac_capacity - ac_w)   # остаток мощности дизеля — на заряд АКБ
        battery_request = charge_offer - deficit           # deficit > 0 только если PV целиком в нагрузке

        ocv = self.battery.open_circuit_voltage()
        batt_v, batt_a, batt_w = self.battery.step(battery_request, gdt, discharge_limit_a, charge_allowed)
        pv_w = pv_avail
        if battery_request > 0:
            accepted = max(0.0, batt_w)
            pv_charge = min(pv_surplus, accepted)
            if generator_feeds:
                ac_w += max(0.0, accepted - pv_charge)
            if pv_surplus - pv_charge > 5.0:
                pv_w = pv_to_load + pv_charge
                if mppt_state == "TRACKING":
                    mppt_state = "LIMITED"  # АКБ не принимает излишек — MPPT уходит с точки

        unserved = 0.0
        if battery_request < 0:
            # недоданное АКБ (по току, без учёта потерь на внутреннем сопротивлении)
            missing = -battery_request - max(0.0, -batt_a) * ocv
            if missing > 1.0:
                if use_ac and ac_source == "GRID":
                    ac_w += missing
                else:
                    unserved = missing
        if unserved > 1.0:
            self._deficit_s += gdt
        else:
            self._deficit_s = 0.0
        at_cutoff = self.battery.soc <= self.battery.discharge_cutoff_soc + 1e-6
        if unserved > 1.0 and error_code is None and (at_cutoff or self._deficit_s >= 60.0):
            error_code = "E04"
        elif error_code == "E04" and (ac_ok or self.battery.soc > self.battery.discharge_cutoff_soc + 0.05):
            error_code = None  # восстановление после разряда — автоматически

        output_off = output_blocked or error_code in ("E02", "E04", "E07")
        output_v = 0.0 if output_off else 230.0 + self.noise(1.0)
        served_load = 0.0 if output_off else load_w

        # --- тепловой режим и вентилятор ---
        fan_fault = node["control_fan_fault"]
        fan_mode = node["fan_mode"]
        temp = node["inverter_temp_c"]
        if fan_fault == FAN_FAULT_STOPPED:
            fan_running = False
        elif fan_mode == "AUTO":
            fan_running = temp > 45.0 or (node["fan_state"] in (ON, FAULT) and temp > 38.0)
        else:
            fan_running = fan_mode == "ON"
        r = served_load / self.rated_w + 0.1 * pv_w / self.rated_w
        rise = (8.0 + 45.0 * r) if fan_running else (20.0 + 90.0 * r)
        target_t = self.env.indoor.temperature_c + rise
        temp = relax(temp, target_t, 660, gdt) + self.noise(0.15)
        if fan_fault:
            fan_state = FAULT
        else:
            fan_state = ON if fan_running else OFF
        wear = float(node["control_fan_wear"] or 0.0)
        fan_current = 0.0 if not fan_running else 0.25 * (1.0 + 1.5 * wear) + abs(self.noise(0.005))

        # --- балансировка ---
        if balancing == "BALANCING":
            self._balancing_remaining_s -= gdt
            if self._balancing_remaining_s <= 0:
                balancing = "COMPLETED"
                self.emit("inverter.balancing_completed")

        if error_code and error_code != node["error_code"]:
            self.emit("inverter.error", Severity.CRITICAL, error_code=error_code,
                      error_text=INVERTER_ERRORS[error_code])

        pv_v = 0.0 if irradiance < 3.0 else self.pv_vmp_v * (1.22 if pv_w == 0 else 1.0) + self.noise(0.3)
        if sense_k is not None:
            pv_v *= float(sense_k)

        u.update({
            "pv_voltage_v": round(max(0.0, pv_v), 2),
            "pv_power_w": round(pv_w, 1),
            "battery_voltage_v": round(batt_v, 2),
            "battery_current_a": round(batt_a, 2),
            "battery_power_w": round(batt_w, 1),
            "battery_soc_pct": round(self.battery.soc * 100, 2),
            "battery_capacity_kwh": round(self.battery.nominal_kwh, 2),
            "load_power_w": round(served_load, 1),
            "output_voltage_v": round(output_v, 1),
            "output_limit_pct": round(limit_pct, 1),
            "grid_power_w": round(ac_w if ac_source == "GRID" else 0.0, 1),
            "generator_power_w": round(ac_w if ac_source == "GENERATOR" else 0.0, 1),
            "ac_source": ac_source,
            "grid_state": grid_state,
            "grid_voltage_v": round(ext.grid_voltage_v, 1),
            "mppt_state": mppt_state,
            "battery_charge_blocked": not charge_allowed,
            "power_deficit_w": round(unserved, 1),
            "inverter_temp_c": round(temp, 1),
            "fan_state": fan_state,
            "fan_current_a": round(fan_current, 3),
            "error": error_code is not None,
            "error_code": error_code,
            "error_text": INVERTER_ERRORS.get(error_code) if error_code else None,
            "balancing_state": balancing,
        })
        return u

    # ---- управление ----

    @control("start_cell_balancing")
    def _start_balancing(self, node, value):
        if node["balancing_state"] == "BALANCING":
            raise ControlError("Балансировка уже выполняется")
        minutes = 45.0 if value is None else parse_number(value, 1.0, 240.0, "Длительность балансировки, мин")
        self._balancing_remaining_s = minutes * 60.0
        return {"balancing_state": "BALANCING"}

    @control("cancel_balancing")
    def _cancel_balancing(self, node, value):
        if node["balancing_state"] != "BALANCING":
            raise ControlError("Балансировка не выполняется")
        self._balancing_remaining_s = 0.0
        return {"balancing_state": "CANCELLED"}

    @control("set_mode_hybrid", "hybrid")
    def _mode_hybrid(self, node, value):
        return {"mode": "HYBRID", **self._clear_grid_sense(node)}

    @control("set_mode_grid_only", "grid_only")
    def _mode_grid_only(self, node, value):
        return {"mode": "GRID_ONLY"}

    @control("set_mode")
    def _set_mode(self, node, value):
        mode = parse_choice(value, INVERTER_MODES, "Режим инвертора")
        return {"mode": mode, **(self._clear_grid_sense(node) if mode == "HYBRID" else {})}

    @staticmethod
    def _clear_grid_sense(node: dict[str, Any]) -> dict[str, Any]:
        # Переинициализация логики режимов сбрасывает зависший контроль сети (№44)
        return {"control_grid_sense_fault": False} if node.get("control_grid_sense_fault") else {}

    @control("enable_mppt")
    def _enable_mppt(self, node, value):
        return {"mppt_enabled": True}

    @control("disable_mppt")
    def _disable_mppt(self, node, value):
        return {"mppt_enabled": False, "mppt_state": "OFF"}

    @control("reset_error")
    def _reset_error(self, node, value):
        """Сброс ошибки. E02 не снимается, пока инвертор не остыл; E09 вернётся
        через 2 мин, если причина (конденсат) не ушла. Сбрасывает также сбои
        измерительных цепей (ложный отказ вентилятора, занижение PV, контроль сети)."""
        u: dict[str, Any] = {"control_pv_sense_k": None, "control_grid_sense_fault": False}
        if node["control_fan_fault"] == FAN_FAULT_SENSOR:
            u["control_fan_fault"] = None
        code = node["error_code"]
        if code == "E02" and node["inverter_temp_c"] >= self.E02_RECOVER_C:
            raise ControlError("E02: инвертор не остыл (≥65 °C), сброс невозможен")
        if code == "E09" or node["control_isolation_fault"]:
            self._e09_rearm_s = self.E09_REARM_S
        if code == "E07":
            self._e07_remaining_s = 0.0
        self._overload_s = 0.0
        u.update({"error": False, "error_code": None, "error_text": None})
        return u

    @control("set_fan")
    def _set_fan(self, node, value):
        return {"fan_mode": parse_choice(value, ("AUTO", "ON", "OFF"), "Режим вентилятора")}

    @control("replace_fan", physical=True)
    def _replace_fan(self, node, value):
        """Замена вентилятора (физически, обычно после смены)."""
        return {"control_fan_fault": None, "control_fan_wear": 0.0}


# ---------------------------------------------------------------------------
# 1.3 Аккумуляторная батарея
# ---------------------------------------------------------------------------

@register_node_type("battery")
class Battery(SandboxNode):
    """
    Данные BMS. Две схемы работы:
      - с правилами зависимостей узел зеркалирует модель АКБ инвертора
        (control_soc_pct + control_power_w) — единая истина об энергии;
      - без правил АКБ сама оценивает поток энергии (PV минус типовая нагрузка)
        или следует control_power_w.

    Температурная модель (Д8): АКБ стоит в техпомещении, его температура — смесь
    наружной и внутренней, доля наружной растёт с притоком свежего воздуха
    (control_fresh_air_share). LiFePO4 нельзя заряжать ниже 0 °C — BMS запрещает
    заряд (charge_allowed=False, К11).
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
            "temperature_c": 20.0,
            "charge_allowed": True,
            "control_power_w": None,
            "control_soc_pct": None,
            "control_fresh_air_share": None,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        m = self.model
        mirrored = node.get("control_soc_pct") is not None
        if mirrored:
            m.soc = clamp(float(node["control_soc_pct"]) / 100.0, 0.0, 1.0)
            power = float(node["control_power_w"] or 0.0)
            voltage = m.open_circuit_voltage() + power / m.open_circuit_voltage() * m.INTERNAL_RESISTANCE_OHM
            current = power / voltage if voltage > 0 else 0.0
            m.throughput_ah += abs(current) * gdt / 3600.0
            m.capacity_fade = min(0.4, m.throughput_ah / (2 * m.nominal_capacity_ah) * 0.0002)
        else:
            if node["control_power_w"] is not None:
                request = float(node["control_power_w"])
            else:
                pv, _ = pv_output_w(self.env, self.pv_rated_w)
                request = pv * 0.95 - max(50.0, dome_base_load_w(self.hour) + self._load_noise.step(gdt))
            voltage, current, power = m.step(request, gdt)
        state = "CHARGING" if current > 0.2 else ("DISCHARGING" if current < -0.2 else "IDLE")

        # Температура АКБ в техпомещении
        share = node.get("control_fresh_air_share")
        share = 0.5 if share is None else float(share)
        w_out = 0.3 + 0.8 * clamp(share, 0.0, 1.0)
        target = w_out * self.env.outdoor.temperature_c + (1.0 - w_out) * self.env.indoor.temperature_c \
            + 0.03 * abs(current)
        temp = relax(node["temperature_c"], target, 900.0, gdt)
        charge_allowed = temp >= 2.0 if not node["charge_allowed"] else temp >= 0.0
        if node["charge_allowed"] and not charge_allowed:
            self.emit("battery.charge_blocked", Severity.WARNING, reason="LOW_TEMPERATURE",
                      temperature_c=round(temp, 1))

        # Пассивная балансировка BMS в конце заряда — разбаланс ячеек уменьшается
        delta = node["cell_voltage_delta_mv"]
        balancing = state == "CHARGING" and m.soc > 0.95
        delta = relax(delta, 3.0, 1800, gdt) if balancing else delta + abs(current) * gdt / 3600.0 * 0.05
        delta = clamp(delta + self.noise(0.3), 1.0, 150.0)

        was_low = node["soc_pct"] < 25.0
        if m.soc * 100 < 25.0 and not was_low:
            self.emit("battery.low", Severity.WARNING, soc_pct=round(m.soc * 100, 1))

        return {
            "soc_pct": round(m.soc * 100, 2 if mirrored else 1),
            "voltage_v": round(voltage + self.noise(0.01), 2),
            "current_a": round(current, 2),
            "power_w": round(power, 1),
            "estimated_capacity_kwh": round(m.actual_capacity_ah * m.nominal_voltage / 1000.0, 3),
            "state": state,
            "balancing_state": "BALANCING" if balancing else "IDLE",
            "cell_voltage_delta_mv": round(delta, 1),
            "temperature_c": round(temp, 2),
            "charge_allowed": charge_allowed,
        }


# ---------------------------------------------------------------------------
# 1.4 Ветрогенератор
# ---------------------------------------------------------------------------

@register_node_type("wind_turbine")
class WindTurbine(SandboxNode):
    """Собственный анемометр на мачте (wind_speed_ms) — независимый источник
    скорости ветра для сверки с метеостанцией (№18)."""
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
    Работает на AC-вход инвертора. Входы правил: control_load_w (сколько берёт
    инвертор), control_fuel_available (False — топливо закончилось),
    control_fuel_quality (качество топлива в баке), control_fuel_temp_c.

    Запуск не удаётся (START_FAILURE), если в топливе вода (fuel_quality = WATER, К1)
    или топливо загустело на морозе (< −10 °C) без предпускового подогрева (К11).
    Подогрев — действие preheat (2 мин).
    """
    title = "Дизельный генератор"
    system = "Энергетика"
    category = "power"

    GEL_TEMP_C = -10.0
    PREHEAT_S = 120.0

    def __init__(self, node_id: str, rated_w: float = 3000.0, start_failure_prob: float = 0.03,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.rated_w = rated_w
        self.start_failure_prob = start_failure_prob
        self._load = OUProcess(0.55, 0.1, 1200, self.rng)
        self._preheat_s = 0.0
        self._preheated_s = 0.0

    def initial_state(self) -> dict[str, Any]:
        return {
            "power_w": 0.0,
            "state": OFF,
            "fault_code": None,       # START_FAILURE | NO_FUEL | OVERHEAT
            "fault_reason": None,     # WATER_IN_FUEL | FUEL_GEL | CRANK
            "start_attempts": 0,
            "preheat_state": "OFF",   # OFF | HEATING | READY
            "engine_temp_c": 20.0,
            "fuel_consumption_l_h": 0.0,
            "runtime_h": 0.0,
            "control_load_w": None,
            "control_fuel_available": True,
            "control_fuel_quality": None,
            "control_fuel_temp_c": None,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        state, fault = node["state"], node["fault_code"]
        ambient = self.env.outdoor.temperature_c

        if self._preheat_s > 0:
            self._preheat_s -= gdt
            if self._preheat_s <= 0:
                self._preheated_s = 600.0
        elif self._preheated_s > 0:
            self._preheated_s -= gdt
        preheat_state = "HEATING" if self._preheat_s > 0 else ("READY" if self._preheated_s > 0 else "OFF")

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

        consumption = (0.4 + 0.28 * power / 1000.0) if state == ON else 0.0
        return {
            "power_w": round(power, 1),
            "state": state,
            "fault_code": fault,
            "preheat_state": preheat_state,
            "engine_temp_c": round(temp, 1),
            "fuel_consumption_l_h": round(consumption, 3),
            "runtime_h": round(node["runtime_h"] + (gdt / 3600.0 if state == ON else 0.0), 4),
        }

    def available_power_w(self, node: dict[str, Any]) -> float:
        """Мощность, которую генератор может отдать инвертору сейчас."""
        if node["state"] != ON:
            return 0.0
        return self.rated_w * (0.6 if node["engine_temp_c"] < 60.0 else 1.0)

    @control("turn_on", "on", "start", "enable")
    def _turn_on(self, node, value):
        if node["state"] == ON:
            return None
        attempts = node.get("start_attempts", 0) + 1
        if not node["control_fuel_available"]:
            self.emit("dizel.fault", Severity.CRITICAL, fault_code="NO_FUEL")
            return {"state": FAULT, "fault_code": "NO_FUEL", "fault_reason": None, "start_attempts": attempts}
        reason = None
        if node.get("control_fuel_quality") == "WATER":
            reason = "WATER_IN_FUEL"
        elif node.get("control_fuel_temp_c") is not None and float(node["control_fuel_temp_c"]) < self.GEL_TEMP_C \
                and self._preheated_s <= 0:
            reason = "FUEL_GEL"
        elif self.rng.random() < self.start_failure_prob:
            reason = "CRANK"
        if reason:
            self.emit("dizel.fault", Severity.WARNING, fault_code="START_FAILURE", reason=reason)
            return {"state": FAULT, "fault_code": "START_FAILURE", "fault_reason": reason, "start_attempts": attempts}
        self.emit("dizel.started", Severity.INFO)
        return {"state": ON, "fault_code": None, "fault_reason": None, "start_attempts": attempts}

    @control("turn_off", "off", "stop", "disable", "reset_alarm")
    def _turn_off(self, node, value):
        return {"state": OFF, "fault_code": None, "fault_reason": None, "power_w": 0.0}

    @control("preheat")
    def _preheat(self, node, value):
        """Предпусковой подогрев топлива и двигателя (2 мин)."""
        if node["state"] == ON:
            raise ControlError("Генератор уже работает")
        self._preheat_s = self.PREHEAT_S
        return {"preheat_state": "HEATING"}


# ---------------------------------------------------------------------------
# 1.6 Топливный бак
# ---------------------------------------------------------------------------

@register_node_type("fuel_tank")
class FuelTank(SandboxNode):
    """
    Вход для правил: control_consumption_l_h (расход дизеля). Без правил расход 0.
    Вода в топливе медленно накапливается от конденсата (чем больше воздуха в баке
    и выше влажность — тем быстрее). Перекачка из резервной ёмкости (чистое
    топливо) разбавляет воду — способ вернуть качество до запуска дизеля (К1).
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

    @staticmethod
    def _quality(water: float, contamination: float) -> str:
        return "CONTAMINATED" if contamination > 5.0 else ("WATER" if water > 500.0 else "GOOD")

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        rate = float(node["control_consumption_l_h"] or 0.0)
        volume = max(0.0, node["volume_l"] - rate * gdt / 3600.0)
        level = volume / self.capacity_l

        humidity = self.env.outdoor.humidity_pct / 100.0
        water = node["water_content_ppm"] + (1.0 - level) * humidity * 6.0 * gdt / 86400.0
        contamination = node["contamination_pct"] + 0.01 * gdt / 86400.0
        temp = relax(node["fuel_temp_c"], self.env.outdoor.temperature_c, 1800.0, gdt)

        quality = self._quality(water, contamination)
        if quality != "GOOD":
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
            "volume_l": round(volume, 3),
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
        water = node["water_content_ppm"] * node["volume_l"] / volume   # резервное топливо сухое
        quality = self._quality(water, node["contamination_pct"])
        return {"volume_l": round(volume, 2), "reserve_l": round(node["reserve_l"] - amount, 2),
                "level_pct": round(volume / self.capacity_l * 100, 1),
                "water_content_ppm": round(water, 3), "fuel_quality": quality,
                "state": "CONTAMINATED" if quality != "GOOD" else "NORMAL"}


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
    return 12.0  # светильники аварийного освещения + подзаряд их аккумуляторов


def _load_fume(hour: float) -> float:
    return 95.0 if 9 <= hour < 19 else 0.0


def _load_none(hour: float) -> float:
    return 0.0


# (номер, назначение, номинал автомата, А, профиль нагрузки, Вт, включена при старте)
PANEL_LINES = [
    (1, "Производственный отдел", 16.0, _load_production, True),
    (2, "ЧПУ-фрезер", 10.0, _load_cnc, True),
    (3, "3D-принтер", 10.0, _load_printer, True),
    (4, "Обычное освещение", 10.0, _load_lighting, True),
    (5, "Аварийное освещение", 6.0, _load_emergency, False),
    (6, "Вытяжка FabLab", 10.0, _load_fume, True),
    (7, "Резервная линия", 16.0, _load_none, True),
    (8, "Резервная линия / фантомное потребление", 16.0, _load_none, True),
]

LINE_ON, LINE_OFF, LINE_TRIPPED, LINE_RCD_TRIP = "ON", "OFF", "TRIPPED", "RCD_TRIP"

# Рабочее место «Производственный отдел» (линия 1): что делает Оператор
WORKSTATION_LOAD_W = {"IDLE": 120.0, "SOLDERING": 340.0, "HOT_AIR": 760.0, "OFF": 0.0}
# Пусковые токи при подаче питания: линия -> множитель (свет, двигатели, БП, нагреватели)
INRUSH_K = {1: 2.0, 2: 2.0, 3: 2.2, 4: 3.0, 5: 1.5, 6: 4.0, 7: 1.0, 8: 1.0}
INRUSH_S = 8.0
RCD_THRESHOLD_MA = 30.0


@register_node_type("smart_panel")
class SmartPanel(SandboxNode):
    """
    Щиток на 8 линий. Телеметрия линий — список `lines` (индекс 0 = линия 1).

    Входы для правил и кризисов:
        control_line_loads_w: {"2": 250.0, ...} — фактическая нагрузка линии
            (от ЧПУ, принтера, вытяжки); линии без записи используют профиль;
        control_phantom_load_w: добавочная «фантомная» нагрузка на линию 8 (№1);
        control_bus_powered: есть ли напряжение на выходе инвертора (Д19);
        control_bus_voltage_v: напряжение выхода инвертора;
        control_leakage_ma: {"2": 12.0} — ток утечки линии (Д15, №26); при ≥ 30 мА — УЗО;
        control_workstation: что делает Оператор на рабочем месте линии 1
            (IDLE | SOLDERING | HOT_AIR | OFF; None — суточный профиль).

    Пусковые токи: при подаче напряжения на шину (после блэкаута) все включённые
    линии 8 с потребляют с множителем INRUSH_K; при включении одной линии — только она.
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
        self._inrush: dict[int, float] = {}

    def initial_state(self) -> dict[str, Any]:
        lines = []
        for number, name, rated_a, _, enabled in PANEL_LINES:
            lines.append({
                "line": number,
                "name": name,
                "rated_current_a": rated_a,
                "state": LINE_ON if enabled else LINE_OFF,
                "voltage_v": 230.0 if enabled else 0.0,
                "current_a": 0.0,
                "power_w": 0.0,
                "leakage_ma": 0.8,
                "protection_state": "ARMED",   # ARMED | TRIPPED | RCD_TRIPPED
                "alarm": False,
                "alarm_code": None,            # OVERCURRENT | RCD_LEAKAGE | EMULATED | ...
            })
        return {
            "lines": lines,
            "bus_voltage_v": 230.0,
            "bus_powered": True,
            "total_power_w": 0.0,
            "total_current_a": 0.0,
            "lines_on_count": sum(1 for l in lines if l["state"] == LINE_ON),
            "control_line_loads_w": {},
            "control_phantom_load_w": 0.0,
            "control_bus_powered": True,
            "control_bus_voltage_v": None,
            "control_leakage_ma": {},
            "control_workstation": None,
        }

    def line_demand_w(self, number: int, hour: float, node: dict[str, Any], noise_k: float = 1.0) -> float:
        overrides = node.get("control_line_loads_w") or {}
        if str(number) in overrides:
            demand = float(overrides[str(number)])
        elif number == 1 and node.get("control_workstation"):
            demand = WORKSTATION_LOAD_W.get(node["control_workstation"], 120.0) * (0.97 + 0.03 * noise_k)
        else:
            demand = PANEL_LINES[number - 1][3](hour) * noise_k
        if number == 8:
            demand += float(node.get("control_phantom_load_w") or 0.0)
        return max(0.0, demand)

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        hour = self.hour
        powered = node.get("control_bus_powered") is not False
        if powered and not node.get("bus_powered", True):
            # напряжение вернулось — все включённые нагрузки стартуют разом
            for l in node["lines"]:
                if l["state"] == LINE_ON:
                    self._inrush[l["line"]] = INRUSH_S
        leakage_in = node.get("control_leakage_ma") or {}

        demands = []
        for idx, (number, _, _, _, _) in enumerate(PANEL_LINES):
            k = clamp(self._noise[idx].step(gdt), 0.3, 2.0)
            demand = self.line_demand_w(number, hour, node, k)
            if self._inrush.get(number, 0.0) > 0:
                demand *= INRUSH_K.get(number, 1.0)
                self._inrush[number] -= gdt
            demands.append(demand)

        base_v = node.get("control_bus_voltage_v")
        base_v = 230.0 if base_v is None else float(base_v)
        live_current = sum(d / 230.0 for d, l in zip(demands, node["lines"]) if l["state"] == LINE_ON)
        bus_v = (base_v + self.noise(1.2) - 0.08 * live_current) if powered else 0.0

        lines, total_p, total_i = [], 0.0, 0.0
        for line, demand in zip(node["lines"], demands):
            line = dict(line)
            number = line["line"]
            leak = max(0.0, 0.8 + self.noise(0.25) + float(leakage_in.get(str(number), 0.0)))
            live = line["state"] == LINE_ON and powered
            if live:
                current = demand / (bus_v * self.POWER_FACTOR)
                if current > line["rated_current_a"]:
                    line.update(state=LINE_TRIPPED, protection_state="TRIPPED",
                                alarm=True, alarm_code="OVERCURRENT")
                    self.emit("smart_panel.line_tripped", Severity.CRITICAL, line=number,
                              reason="OVERCURRENT", current_a=round(current, 2))
                elif leak >= RCD_THRESHOLD_MA:
                    line.update(state=LINE_RCD_TRIP, protection_state="RCD_TRIPPED",
                                alarm=True, alarm_code="RCD_LEAKAGE")
                    self.emit("smart_panel.line_tripped", Severity.CRITICAL, line=number,
                              reason="RCD_LEAKAGE", leakage_ma=round(leak, 1))
                elif self.happens(self.nuisance_trip_rate_per_hour, gdt):
                    line.update(state=LINE_RCD_TRIP, protection_state="RCD_TRIPPED",
                                alarm=True, alarm_code="RCD_LEAKAGE")
                    self.emit("smart_panel.line_tripped", Severity.WARNING, line=number,
                              reason="RCD_LEAKAGE")
            live = line["state"] == LINE_ON and powered
            if live:
                current = demand / (bus_v * self.POWER_FACTOR)
                line.update(voltage_v=round(bus_v, 1), current_a=round(current, 3), power_w=round(demand, 1),
                            leakage_ma=round(leak, 2))
                total_p += demand
                total_i += current
            else:
                line.update(voltage_v=0.0, current_a=0.0, power_w=0.0,
                            leakage_ma=round(leak if line["state"] == LINE_ON else 0.0, 2))
            lines.append(line)

        return {
            "lines": lines,
            "bus_voltage_v": round(bus_v, 1),
            "bus_powered": powered,
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
        if line["state"] != LINE_ON:
            self._inrush[number] = INRUSH_S
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
        self._inrush[number] = INRUSH_S
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

    # ---- физические действия Оператора ----

    @control("set_workstation", physical=True)
    def _set_workstation(self, node, value):
        """Что делает Оператор на рабочем месте (линия 1): IDLE | SOLDERING | HOT_AIR | OFF."""
        return {"control_workstation": parse_choice(value, tuple(WORKSTATION_LOAD_W), "Режим рабочего места")}

    @control("inspect_line", physical=True)
    def _inspect_line(self, node, value):
        """Осмотр и просушка разъёма/кабеля линии Оператором — утечка устранена (№26)."""
        number = self._line_number(value)
        leak = dict(node.get("control_leakage_ma") or {})
        leak[str(number)] = 2.2
        self.emit("smart_panel.line_inspected", Severity.INFO, line=number)
        return {"control_leakage_ma": leak}

    # ---- реальные реле (Д7, Д19) ----

    def real_command(self, store: StateStore, action: str, value: Any = None) -> list[tuple[str, Any]]:
        """Реле Tuya умеют только вкл/выкл: срабатывание и взвод защиты эмулируются
        состоянием узла + командой реле."""
        if action in ("reset_protection", "emulate_protection_trip", "reset_line_error"):
            node = store.get_node(self.node_id)
            number = self._line_number(value)
            updates = getattr(self, {"reset_protection": "_reset_protection",
                                     "emulate_protection_trip": "_emulate_trip",
                                     "reset_line_error": "_reset_error"}[action])(node, value)
            store.update(self.node_id, **updates)
            if action == "reset_protection":
                return [("line_on", number)]
            if action == "emulate_protection_trip":
                return [("line_off", number)]
            return []
        return [(action, value)]

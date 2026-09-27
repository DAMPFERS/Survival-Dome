# dome_simulator/nodes/climate.py
"""
Климат, качество воздуха, вентиляция и герметизация (разделы 4.1–4.2, 5.1, 8, 10
реестра dome_sandbox_nodes.md): climate_sensor, climate_sensor_extra,
air_quality_sensor, fume_extraction, supply_ventilation, dome_sealing,
thermal_insulation (МС-ТЮК).

Все датчики меряют один и тот же воздух купола — env.indoor. Правила и кризисы
будут менять env.indoor (например, вытяжка снижает VOC), а датчики это увидят.
"""
from __future__ import annotations

from typing import Any, Optional

from ..environment import OUProcess, clamp, dew_point_c, relax
from ..events import Severity
from .base import register_node_type
from .sandbox import ControlError, SandboxNode, SensorNode, control, parse_bool, parse_choice, parse_number

ON, OFF, FAULT = "ON", "OFF", "FAULT"


# ---------------------------------------------------------------------------
# 4.1 / 8.3 Датчики климата
# ---------------------------------------------------------------------------

@register_node_type("climate_sensor")
class ClimateSensor(SensorNode):
    title = "Датчик климата"
    system = "Климат"
    category = "climate"

    TEMP_SIGMA, HUM_SIGMA, CO2_SIGMA = 0.1, 0.8, 12.0

    def __init__(self, node_id: str, zone: str = "FABLAB", glitch_rate_per_hour: float = 0.01,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, glitch_rate_per_hour, seed)
        self.zone = zone

    def initial_state(self) -> dict[str, Any]:
        i = self.env.indoor
        return {
            "zone": self.zone,
            "temperature_c": round(i.temperature_c, 2),
            "humidity_pct": round(i.humidity_pct, 1),
            "co2_ppm": round(i.co2_ppm),
            "dew_point_c": round(dew_point_c(i.temperature_c, i.humidity_pct), 2),
        }

    def true_values(self, gdt: float) -> tuple[float, float, float]:
        i = self.env.indoor
        return i.temperature_c, i.humidity_pct, i.co2_ppm

    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> dict[str, Any]:
        t, h, c = self.true_values(gdt)
        t += self.noise(self.TEMP_SIGMA * noise_k)
        h = clamp(h + self.noise(self.HUM_SIGMA * noise_k), 0.0, 100.0)
        c = max(350.0, c + self.noise(self.CO2_SIGMA * noise_k))
        return {
            "temperature_c": round(t, 2),
            "humidity_pct": round(h, 1),
            "co2_ppm": round(c),
            "dew_point_c": round(dew_point_c(t, h), 2),
        }


@register_node_type("climate_sensor_extra")
class ClimateSensorExtra(ClimateSensor):
    """
    Избыточный датчик: меряет тот же воздух, но с инерцией (reading_delay_s)
    и медленно накапливающимся дрейфом калибровки. Сравнение с основным
    датчиком — способ заметить неисправность.
    Кризисный вход control_co2_drift_ppm_min — ускоренный дрейф CO2 (К12);
    накопленный дрейф виден в drift_co2_ppm и снижает confidence.
    """
    title = "Дополнительный датчик климата"

    def __init__(self, node_id: str, zone: str = "FABLAB", reading_delay_s: float = 900.0,
                 drift_rate_c_per_day: float = 0.15, glitch_rate_per_hour: float = 0.02,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, zone, glitch_rate_per_hour, seed)
        self.reading_delay_s = reading_delay_s
        self.drift_rate_c_per_day = drift_rate_c_per_day
        self._lagged: Optional[list[float]] = None
        self._drift_sign = self.rng.choice((-1.0, 1.0))
        self._drift = [0.0, 0.0, 0.0]  # температура, влажность, CO2 — накапливается без округления

    def initial_state(self) -> dict[str, Any]:
        state = super().initial_state()
        state.update(reading_delay_s=self.reading_delay_s, drift_temp_c=0.0,
                     drift_humidity_pct=0.0, drift_co2_ppm=0.0, control_co2_drift_ppm_min=0.0)
        return state

    def true_values(self, gdt: float) -> tuple[float, float, float]:
        actual = list(super().true_values(gdt))
        if self._lagged is None:
            self._lagged = actual
        self._lagged = [relax(old, new, self.reading_delay_s, gdt) for old, new in zip(self._lagged, actual)]
        return tuple(self._lagged)  # type: ignore[return-value]

    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> dict[str, Any]:
        days = gdt / 86400.0
        rate = self.drift_rate_c_per_day
        self._drift[0] += self._drift_sign * rate * days + self.noise(0.002)
        self._drift[1] += self._drift_sign * rate * 3.0 * days
        self._drift[2] += self._drift_sign * rate * 50.0 * days
        self._drift[2] += float(node.get("control_co2_drift_ppm_min") or 0.0) * gdt / 60.0
        drift_t, drift_h, drift_c = self._drift
        m = super().measure(gdt, node, noise_k)
        m["temperature_c"] = round(m["temperature_c"] + drift_t, 2)
        m["humidity_pct"] = round(clamp(m["humidity_pct"] + drift_h, 0.0, 100.0), 1)
        m["co2_ppm"] = round(m["co2_ppm"] + drift_c)
        m["dew_point_c"] = round(dew_point_c(m["temperature_c"], m["humidity_pct"]), 2)
        m.update(drift_temp_c=round(drift_t, 3), drift_humidity_pct=round(drift_h, 2), drift_co2_ppm=round(drift_c, 1))
        return m

    def sensor_condition(self, gdt, node):
        state, confidence, noise_k = super().sensor_condition(gdt, node)
        if state == "OK":  # чем больше дрейф — тем ниже достоверность
            confidence = round(clamp(confidence - abs(node["drift_temp_c"]) * 0.2
                                     - abs(node["drift_co2_ppm"]) / 2500.0, 0.1, 1.0), 3)
        return state, confidence, noise_k

    @control("recalibrate", "calibrate")
    def _recalibrate(self, node, value):
        self._drift = [0.0, 0.0, 0.0]
        return {"drift_temp_c": 0.0, "drift_humidity_pct": 0.0, "drift_co2_ppm": 0.0}


# ---------------------------------------------------------------------------
# 4.2 Датчик качества воздуха CO/VOC
# ---------------------------------------------------------------------------

@register_node_type("air_quality_sensor")
class AirQualitySensor(SensorNode):
    """
    MOX-сенсор зоны (по умолчанию FABLAB): CO, индекс VOC и опасные газы
    внутри купола (chem_ppm — аммиак и др., проникшие снаружи, К5).
    После включения сенсору нужен прогрев (warmup_s) — достоверность ниже.
    """
    title = "Датчик качества воздуха CO/VOC"
    system = "Климат"
    category = "climate"

    def __init__(self, node_id: str, zone: str = "FABLAB", warmup_s: float = 900.0,
                 glitch_rate_per_hour: float = 0.01, seed: Optional[int] = None) -> None:
        super().__init__(node_id, glitch_rate_per_hour, seed)
        self.zone = zone
        self.warmup_s = warmup_s
        self._uptime_s = 0.0

    def initial_state(self) -> dict[str, Any]:
        return {"zone": self.zone, "co_ppm": 0.0, "voc_index": 100, "chem_ppm": 0.0, "warming_up": True}

    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> dict[str, Any]:
        self._uptime_s += gdt
        warming = self._uptime_s < self.warmup_s
        k = noise_k * (3.0 if warming else 1.0)
        air = self.env.zone_air(self.zone)
        chem = self.env.indoor.chem_ppm if self.zone.upper() in ("FABLAB", "MAIN") else 0.0
        return {
            "co_ppm": round(max(0.0, air.co_ppm + self.noise(0.15 * k)), 2),
            "voc_index": int(clamp(air.voc_index + self.noise(4.0 * k), 1, 500)),
            "chem_ppm": round(max(0.0, chem + self.noise(0.05 * k)), 2),
            "warming_up": warming,
        }

    def sensor_condition(self, gdt, node):
        state, confidence, noise_k = super().sensor_condition(gdt, node)
        if state == "OK" and self._uptime_s < self.warmup_s:
            confidence = round(0.5 + 0.45 * self._uptime_s / self.warmup_s, 3)
        return state, confidence, noise_k


# ---------------------------------------------------------------------------
# 5.1 Вытяжка FabLab
# ---------------------------------------------------------------------------

@register_node_type("fume_extraction")
class FumeExtraction(SandboxNode):
    """
    Вход для правил: control_powered (линия 6 щитка).
    Засор фильтра (control_filter_clog_pct, пыль от фрезеровки текстолита)
    нагружает двигатель: при сильном засоре он перегревается (> 85 °C) и
    срабатывает тепловая защита (№17). Продувка фильтра — физическое действие
    clean_filter. Остывание 90 → 60 °C — около 4 минут.
    """
    title = "Вытяжная вентиляция FabLab"
    system = "Вентиляция"
    category = "ventilation"

    MOTOR_TAU_S = 400.0

    def __init__(self, node_id: str, rated_w: float = 95.0, nominal_rpm: float = 2800.0,
                 fault_rate_per_hour: float = 0.003, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.rated_w = rated_w
        self.nominal_rpm = nominal_rpm
        self.fault_rate_per_hour = fault_rate_per_hour
        self._bearing_wear = 0.0

    def initial_state(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "power_w": 0.0,
            "fan_rpm": 0.0,
            "airflow_pct": 0.0,
            "motor_state": "OK",       # OK | OVERHEAT | STALLED
            "motor_temp_c": 22.0,
            "alarm": False,
            "alarm_code": None,
            "control_powered": True,
            "control_filter_clog_pct": 10.0,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        motor_state, alarm_code = node["motor_state"], node["alarm_code"]
        running = node["enabled"] and node["control_powered"] and motor_state == "OK"
        clog = clamp(float(node.get("control_filter_clog_pct") or 0.0), 0.0, 100.0) / 100.0

        if running:
            self._bearing_wear += gdt / (400 * 3600.0)
            if self.happens(self.fault_rate_per_hour * (1.0 + 5.0 * self._bearing_wear), gdt):
                motor_state = "STALLED"
                alarm_code = "MOTOR_STALLED"
                self.emit("fume_extraction.alarm", Severity.WARNING, alarm_code=alarm_code)
                running = False

        ambient = self.env.indoor.temperature_c
        load = (1.0 + 0.3 * self._bearing_wear) * (1.0 + 1.6 * clog ** 2)
        target_temp = ambient + (35.0 * load if running else 0.0)
        motor_temp = relax(node["motor_temp_c"], target_temp, self.MOTOR_TAU_S, gdt) + self.noise(0.2)
        if running and motor_temp > 85.0:
            motor_state, alarm_code, running = "OVERHEAT", "MOTOR_OVERHEAT", False
            self.emit("fume_extraction.alarm", Severity.WARNING, alarm_code=alarm_code,
                      motor_temp_c=round(motor_temp, 1))

        rpm = self.nominal_rpm * (1.0 - 0.1 * self._bearing_wear) * (1.0 - 0.15 * clog) + self.noise(15.0) \
            if running else 0.0
        power = self.rated_w * (1.0 + 0.3 * self._bearing_wear) * (1.0 + 0.25 * clog) * (1.0 + self.noise(0.02)) \
            if running else 0.0
        return {
            "power_w": round(power, 1),
            "fan_rpm": round(max(0.0, rpm)),
            "airflow_pct": round(100.0 * (1.0 - clog ** 1.5) if running else 0.0, 1),
            "motor_state": motor_state,
            "motor_temp_c": round(motor_temp, 1),
            "alarm": alarm_code is not None,
            "alarm_code": alarm_code,
        }

    @control("turn_on", "on", "start", "enable")
    def _turn_on(self, node, value):
        return {"enabled": True}

    @control("turn_off", "off", "stop", "disable")
    def _turn_off(self, node, value):
        return {"enabled": False}

    @control("reset_alarm")
    def _reset_alarm(self, node, value):
        if node["motor_state"] == "OVERHEAT" and node["motor_temp_c"] > 60.0:
            raise ControlError("Двигатель не остыл (>60 °C), сброс аварии невозможен")
        if node["motor_state"] == "STALLED":
            self._bearing_wear *= 0.5  # сброс + ручной прокрут вала
        return {"motor_state": "OK", "alarm": False, "alarm_code": None}

    @control("clean_filter", physical=True)
    def _clean_filter(self, node, value):
        """Продувка/замена фильтра вытяжки (Оператор)."""
        self.emit("fume_extraction.filter_cleaned", Severity.INFO)
        return {"control_filter_clog_pct": 5.0}


# ---------------------------------------------------------------------------
# 8.1 Приточная вентиляция / рекуператор
# ---------------------------------------------------------------------------

VENT_MODES = ("RECIRCULATION", "FRESH_AIR", "MIXED")


@register_node_type("supply_ventilation")
class SupplyVentilation(SandboxNode):
    """
    Параметры приточного воздуха считаются из наружного/внутреннего воздуха
    по режиму и КПД рекуператора. Фильтр забивается пропорционально
    расходу наружного воздуха и запылённости снаружи: в пыльную бурю
    (dust_level ≈ 1) режим FRESH_AIR забивает фильтр примерно за 10 минут (К2).
    fresh_air_m3_h — сколько наружного воздуха подаётся в купол (для правил).
    """
    title = "Приточная вентиляция / рекуператор"
    system = "Вентиляция"
    category = "ventilation"

    def __init__(self, node_id: str, nominal_airflow_m3_h: float = 600.0, rated_w: float = 180.0,
                 recuperator_efficiency: float = 0.75, fault_rate_per_hour: float = 0.002,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.nominal_airflow_m3_h = nominal_airflow_m3_h
        self.rated_w = rated_w
        self.recuperator_efficiency = recuperator_efficiency
        self.fault_rate_per_hour = fault_rate_per_hour

    def initial_state(self) -> dict[str, Any]:
        return {
            "airflow_m3_h": 0.0,
            "fresh_air_m3_h": 0.0,
            "supply_temp_c": 20.0,
            "supply_humidity_pct": 45.0,
            "filter_clog_pct": 12.0,
            "filter_state": "OK",          # OK | DIRTY | REPLACE
            "power_w": 0.0,
            "speed_pct": 60.0,
            "mode": "MIXED",
            "state": ON,
            "alarm": False,
            "alarm_code": None,
            "control_powered": True,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        state, alarm_code = node["state"], node["alarm_code"]
        clog = node["filter_clog_pct"]
        outdoor, indoor = self.env.outdoor, self.env.indoor

        if state == ON and self.happens(self.fault_rate_per_hour, gdt):
            state, alarm_code = FAULT, "FAN_MOTOR"
            self.emit("ventilation.alarm", Severity.WARNING, alarm_code=alarm_code)
        if state == ON and clog >= 98.0:
            state, alarm_code = FAULT, "FILTER_BLOCKED"
            self.emit("ventilation.alarm", Severity.WARNING, alarm_code=alarm_code)

        running = state == ON and node["control_powered"]
        speed = node["speed_pct"] / 100.0
        fresh_share = {"RECIRCULATION": 0.0, "FRESH_AIR": 1.0, "MIXED": 0.5}[node["mode"]]

        if running:
            airflow = self.nominal_airflow_m3_h * speed * (1.0 - 0.5 * (clog / 100.0) ** 2) * (1.0 + self.noise(0.02))
            # приток после рекуператора: наружный воздух подогревается/охлаждается вытяжным
            fresh_t = outdoor.temperature_c + self.recuperator_efficiency * (indoor.temperature_c - outdoor.temperature_c)
            supply_t = fresh_share * fresh_t + (1 - fresh_share) * indoor.temperature_c
            supply_h = fresh_share * clamp(outdoor.humidity_pct * 0.8, 10, 100) + (1 - fresh_share) * indoor.humidity_pct
            dust = outdoor.dust_level
            clog_rate_pct_h = 0.4 + 3.0 * dust + 1400.0 * max(0.0, dust - 0.6)
            clog += fresh_share * airflow / self.nominal_airflow_m3_h * clog_rate_pct_h * gdt / 3600.0
            power = self.rated_w * speed ** 3 * (1.0 + 0.3 * clog / 100.0) + 5.0
        else:
            airflow, power = 0.0, 0.0
            supply_t = relax(node["supply_temp_c"], indoor.temperature_c, 1800, gdt)
            supply_h = node["supply_humidity_pct"]

        clog = clamp(clog, 0.0, 100.0)
        filter_state = "REPLACE" if clog > 85 else ("DIRTY" if clog > 60 else "OK")
        if filter_state != node["filter_state"] and filter_state != "OK":
            self.emit("ventilation.filter", Severity.WARNING, filter_state=filter_state)

        return {
            "airflow_m3_h": round(max(0.0, airflow), 1),
            "fresh_air_m3_h": round(max(0.0, airflow) * fresh_share, 1),
            "supply_temp_c": round(supply_t + self.noise(0.1), 1),
            "supply_humidity_pct": round(clamp(supply_h + self.noise(0.5), 0, 100), 1),
            "filter_clog_pct": round(clog, 2),
            "filter_state": filter_state,
            "power_w": round(power, 1),
            "state": state,
            "alarm": alarm_code is not None,
            "alarm_code": alarm_code,
        }

    @control("turn_on", "on", "start", "enable")
    def _turn_on(self, node, value):
        if node["state"] == FAULT:
            raise ControlError("Авария вентиляции: сначала выполните reset_alarm")
        return {"state": ON}

    @control("turn_off", "off", "stop", "disable")
    def _turn_off(self, node, value):
        return {"state": OFF} if node["state"] != FAULT else None

    @control("set_speed")
    def _set_speed(self, node, value):
        return {"speed_pct": parse_number(value, 10.0, 100.0, "Скорость вентилятора, %")}

    @control("set_mode")
    def _set_mode(self, node, value):
        return {"mode": parse_choice(value, VENT_MODES, "Режим вентиляции")}

    @control("reset_alarm")
    def _reset_alarm(self, node, value):
        if node["alarm_code"] == "FILTER_BLOCKED" and node["filter_clog_pct"] >= 98.0:
            raise ControlError("Фильтр забит: сначала replace_filter")
        return {"state": ON if node["state"] == FAULT else node["state"], "alarm": False, "alarm_code": None}

    @control("replace_filter")
    def _replace_filter(self, node, value):
        return {"filter_clog_pct": 0.0, "filter_state": "OK"}


# ---------------------------------------------------------------------------
# 8.2 Система герметизации купола
# ---------------------------------------------------------------------------

SEALING_ZONES = ("MAIN", "FABLAB", "LIVING", "STORAGE")


@register_node_type("dome_sealing")
class DomeSealing(SandboxNode):
    """
    Клапаны воздухообмена купола. Закрытые клапаны + работающий приток дают
    избыточное давление внутри; утечка оболочки медленно развивается со временем
    и снижает перепад. Клапан может заклинить (FAULT).
    Вход для правил: control_supply_airflow_m3_h (приток, None — типовые 300 м³/ч).
    """
    title = "Система герметизации / клапаны"
    system = "Климат"
    category = "climate"

    VALVE_SPEED_PCT_S = 1.0  # скорость хода клапана, %/игровую секунду

    def __init__(self, node_id: str, valves: int = 4, leak_growth_per_day: float = 0.02,
                 stuck_rate_per_hour: float = 0.002, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.valves = valves
        self.leak_growth_per_day = leak_growth_per_day
        self.stuck_rate_per_hour = stuck_rate_per_hour
        self._stuck: Optional[int] = None

    def initial_state(self) -> dict[str, Any]:
        return {
            "valve_positions_pct": [100.0] * self.valves,  # 100 = полностью открыт
            "valve_targets_pct": [100.0] * self.valves,
            "pressure_diff_pa": 2.0,
            "leak_rate_pct": 3.0,          # условная площадь неплотностей, % от нормы 100
            "leak_detected": False,
            "state": "OPEN",               # OPEN | CLOSED | PARTIAL | FAULT
            "sealed_zone": None,
            "seal_ready": True,
            "fault_code": None,
            "control_supply_airflow_m3_h": None,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        positions = list(node["valve_positions_pct"])
        targets = node["valve_targets_pct"]
        fault_code = node["fault_code"]

        moving = [i for i in range(self.valves) if abs(positions[i] - targets[i]) > 0.5]
        if moving and self._stuck is None and self.happens(self.stuck_rate_per_hour * 20, gdt):
            self._stuck = self.rng.choice(moving)
            fault_code = f"VALVE_{self._stuck + 1}_STUCK"
            self.emit("dome_sealing.fault", Severity.WARNING, fault_code=fault_code)
        step = self.VALVE_SPEED_PCT_S * gdt
        for i in range(self.valves):
            if i == self._stuck:
                continue
            delta = clamp(targets[i] - positions[i], -step, step)
            positions[i] = round(positions[i] + delta, 1)

        leak = node["leak_rate_pct"] + self.leak_growth_per_day * 100 * gdt / 86400.0 * self.rng.uniform(0.5, 1.5)
        leak = clamp(leak, 0.0, 100.0)
        openness = sum(positions) / (100.0 * self.valves)
        supply = node["control_supply_airflow_m3_h"]
        supply = 300.0 if supply is None else float(supply)
        # Перепад: приток против суммарной проводимости (открытые клапаны + неплотности)
        conductance = 0.05 + 3.0 * openness + leak / 25.0
        # (закрыто, без утечек ≈ 60 Па; открыто ≈ 0; предохранительный клапан — 250 Па)
        dp = min(250.0, 1.9e-5 * (supply / conductance) ** 2) + 0.4 * self.env.outdoor.wind_speed_ms * self.noise(0.5)

        if fault_code:
            state = "FAULT"
        elif openness > 0.99:
            state = "OPEN"
        elif openness < 0.01:
            state = "CLOSED"
        else:
            state = "PARTIAL"
        leak_detected = leak > 15.0
        if leak_detected and not node["leak_detected"]:
            self.emit("dome_sealing.leak", Severity.WARNING, leak_rate_pct=round(leak, 1))

        return {
            "valve_positions_pct": positions,
            "pressure_diff_pa": round(dp, 1),
            "leak_rate_pct": round(leak, 5),
            "leak_detected": leak_detected,
            "state": state,
            "seal_ready": fault_code is None and not leak_detected,
            "fault_code": fault_code,
        }

    def _all_targets(self, value: float) -> dict[str, Any]:
        return {"valve_targets_pct": [value] * self.valves}

    @control("open_valves", "open")
    def _open(self, node, value):
        return {**self._all_targets(100.0), "sealed_zone": None}

    @control("close_valves", "close")
    def _close(self, node, value):
        return self._all_targets(0.0)

    @control("seal_zone")
    def _seal_zone(self, node, value):
        zone = parse_choice(value if value is not None else "MAIN", SEALING_ZONES, "Зона герметизации")
        if not node["seal_ready"]:
            raise ControlError("Система не готова к герметизации (утечка или авария клапана)")
        return {**self._all_targets(0.0), "sealed_zone": zone}

    @control("reset_error", "reset_fault")
    def _reset(self, node, value):
        self._stuck = None
        return {"fault_code": None}

    @control("repair_leak")
    def _repair_leak(self, node, value):
        return {"leak_rate_pct": 3.0, "leak_detected": False}


# ---------------------------------------------------------------------------
# 10.1 Теплоизоляционный блок МС-ТЮК
# ---------------------------------------------------------------------------

FANS_IN, FANS_OUT = 4, 2


@register_node_type("thermal_insulation")
class ThermalInsulation(SandboxNode):
    """
    Стендовый теплоизоляционный блок: 4 вентилятора на вдув, 2 на выдув,
    датчики температуры, газа и давления внутри/снаружи, 2 концевика крышки.
    «Снаружи» блока — воздух купола (env.indoor), давление — атмосферное.

    Индексы вентиляторов в командах: 0–3 — вдув, 4–5 — выдув.
    Режимы-«помехи» из реестра:
        random_rpm_mode — вентиляторам постоянно выставляются случайные обороты;
        pwm_distortion  — команды set_fan_pwm искажаются случайным значением.

    Шина опроса (Д16): crc_errors, poll_interval_ms, set_poll_interval.
    Кризисный вход control_bus_collision — конфликт адресов (№22): при опросе
    чаще 500 мс 20–40 % значений приходят пустыми (null), растут CRC-ошибки.
    """
    title = "Теплоизоляционный блок"
    system = "МС-ТЮК"
    category = "climate"

    MAX_RPM = 3000.0
    MIN_SPIN_PWM = 30  # ниже — вентилятор не стартует

    def __init__(self, node_id: str, internal_heat_c: float = 8.0, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.internal_heat_c = internal_heat_c
        self._voc_source = OUProcess(40.0, 15.0, 1800, self.rng)

    def initial_state(self) -> dict[str, Any]:
        base_pa = self.env.outdoor.pressure_hpa * 100.0
        return {
            "fans_in_pwm": [128] * FANS_IN,
            "fans_out_pwm": [128] * FANS_OUT,
            "fans_in_rpm": [0] * FANS_IN,
            "fans_out_rpm": [0] * FANS_OUT,
            "temp_inside_c": 24.0,
            "temp_outside_c": 21.0,
            "limit_switch_1": "CLOSED",
            "limit_switch_2": "CLOSED",
            "voc_inside": 120,
            "voc_outside": 100,
            "pressure_inside_pa": [round(base_pa, 1)] * 2,
            "pressure_outside_pa": round(base_pa, 1),
            "random_rpm_mode": False,
            "pwm_distortion": False,
            "poll_interval_ms": 200,
            "crc_errors": 0,
            "control_bus_collision": False,
        }

    def _rpm(self, pwm: int, current: float, gdt: float) -> float:
        target = 0.0 if pwm < self.MIN_SPIN_PWM else self.MAX_RPM * pwm / 255.0
        rpm = relax(current, target, 3, gdt)
        return max(0.0, rpm + self.noise(12.0)) if rpm > 50 else 0.0

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        pwm_in, pwm_out = list(node["fans_in_pwm"]), list(node["fans_out_pwm"])
        if node["random_rpm_mode"]:
            pwm_in = [self.rng.randint(0, 255) for _ in range(FANS_IN)]
            pwm_out = [self.rng.randint(0, 255) for _ in range(FANS_OUT)]

        rpm_in = [round(self._rpm(p, r, gdt)) for p, r in zip(pwm_in, node["fans_in_rpm"])]
        rpm_out = [round(self._rpm(p, r, gdt)) for p, r in zip(pwm_out, node["fans_out_rpm"])]
        flow_in = sum(rpm_in) / (self.MAX_RPM * FANS_IN)      # 0..1
        flow_out = sum(rpm_out) / (self.MAX_RPM * FANS_OUT)   # 0..1

        outside_t = self.env.indoor.temperature_c
        outside_voc = self.env.indoor.voc_index
        outside_pa = self.env.outdoor.pressure_hpa * 100.0

        exchange = 0.05 + flow_in + flow_out
        lid_open = node["limit_switch_1"] == "OPEN" or node["limit_switch_2"] == "OPEN"
        if lid_open:
            exchange += 3.0
        target_t = outside_t + self.internal_heat_c / (1.0 + 4.0 * exchange)
        inside_t = relax(node["temp_inside_c"], target_t, 1200 / exchange, gdt)
        target_voc = outside_voc + self._voc_source.step(gdt) / (1.0 + 4.0 * exchange)
        inside_voc = relax(node["voc_inside"], target_voc, 900 / exchange, gdt)
        overpressure = 0.0 if lid_open else 35.0 * (flow_in - 0.8 * flow_out)

        u = {
            "fans_in_pwm": pwm_in,
            "fans_out_pwm": pwm_out,
            "fans_in_rpm": rpm_in,
            "fans_out_rpm": rpm_out,
            "temp_inside_c": round(inside_t + self.noise(0.05), 2),
            "temp_outside_c": round(outside_t + self.noise(0.05), 2),
            "voc_inside": int(clamp(inside_voc + self.noise(2.0), 1, 500)),
            "voc_outside": int(clamp(outside_voc + self.noise(2.0), 1, 500)),
            "pressure_inside_pa": [round(outside_pa + overpressure + self.noise(0.8), 1) for _ in range(2)],
            "pressure_outside_pa": round(outside_pa + self.noise(0.8), 1),
        }
        if self.bus_collision_active(node):
            # конфликт адресов: часть ответов не проходит проверку CRC
            # (пропуски значений в телеметрии добавляет слой телеметрии, см. кризис №22)
            loss = self.rng.uniform(0.2, 0.4)
            polls = gdt * 1000.0 / max(node["poll_interval_ms"], 1)
            u["crc_errors"] = int(node["crc_errors"] + max(1, round(polls * loss * 0.05)))
        return u

    @staticmethod
    def bus_collision_active(node: dict[str, Any]) -> bool:
        return bool(node.get("control_bus_collision")) and node.get("poll_interval_ms", 0) < 500

    @control("set_poll_interval")
    def _set_poll_interval(self, node, value):
        """Период опроса шины, мс (50..5000)."""
        return {"poll_interval_ms": int(parse_number(value, 50, 5000, "Период опроса, мс"))}

    # ---- управление ----

    @control("set_fan_pwm")
    def _set_fan_pwm(self, node, value):
        """value: {"fan": 0..5, "pwm": 0..255}."""
        if not isinstance(value, dict):
            raise ControlError('set_fan_pwm: ожидалось {"fan": 0..5, "pwm": 0..255}')
        fan = int(parse_number(value.get("fan"), 0, FANS_IN + FANS_OUT - 1, "Индекс вентилятора"))
        pwm = int(parse_number(value.get("pwm"), 0, 255, "PWM"))
        if node["pwm_distortion"]:
            pwm = int(clamp(pwm + self.rng.randint(-120, 120), 0, 255))
        if fan < FANS_IN:
            fans = list(node["fans_in_pwm"])
            fans[fan] = pwm
            return {"fans_in_pwm": fans}
        fans = list(node["fans_out_pwm"])
        fans[fan - FANS_IN] = pwm
        return {"fans_out_pwm": fans}

    @control("set_random_rpm_mode")
    def _random_mode(self, node, value):
        return {"random_rpm_mode": parse_bool(value)}

    @control("set_pwm_distortion")
    def _distortion(self, node, value):
        return {"pwm_distortion": parse_bool(value)}

# dome_simulator/nodes/external.py
"""
Метеорология и внешние системы (разделы 4.1, 6.1–6.3 реестра dome_sandbox_nodes.md):
weather_station, seysmo, radiation_sensor, chem_sensor.

Все они меряют env.outdoor / env.external. Кризисы следующего этапа
(выброс, землетрясение, радиационный фон) будут менять именно окружение.
"""
from __future__ import annotations

from typing import Any, Optional

from ..environment import clamp
from ..events import Severity
from .base import register_node_type
from .sandbox import ControlError, SensorNode, control, parse_number

COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _compass(deg: float) -> str:
    return COMPASS[int((deg % 360.0) / 45.0 + 0.5) % 8]


# ---------------------------------------------------------------------------
# 6.1 Метеостанция
# ---------------------------------------------------------------------------

@register_node_type("weather_station")
class WeatherStation(SensorNode):
    """
    Показания — наружная погода с шумом приборов. Редкие обрывы связи
    со станцией: data_available=False, значения замирают на последних.
    """
    title = "Метеостанция"
    system = "Метеорология"
    category = "weather"

    def __init__(self, node_id: str, dropout_rate_per_hour: float = 0.02,
                 glitch_rate_per_hour: float = 0.005, seed: Optional[int] = None) -> None:
        super().__init__(node_id, glitch_rate_per_hour, seed)
        self.dropout_rate_per_hour = dropout_rate_per_hour
        self._dropout_s = 0.0

    def initial_state(self) -> dict[str, Any]:
        o = self.env.outdoor
        return {
            "temperature_c": round(o.temperature_c, 1),
            "humidity_pct": round(o.humidity_pct, 1),
            "illuminance_lux": round(o.illuminance_lux),
            "wind_speed_ms": round(o.wind_speed_ms, 1),
            "wind_direction_deg": round(o.wind_direction_deg),
            "wind_direction": _compass(o.wind_direction_deg),
            "pressure_hpa": round(o.pressure_hpa, 1),
            "precipitation_mm_h": o.precipitation_mm_h,
            "data_available": True,
        }

    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> dict[str, Any]:
        if self._dropout_s > 0:
            self._dropout_s -= gdt
            return {"data_available": self._dropout_s <= 0}
        if self.happens(self.dropout_rate_per_hour, gdt):
            self._dropout_s = self.rng.uniform(300.0, 1800.0)
            self.emit("weather_station.no_data", Severity.INFO)
            return {"data_available": False}

        o = self.env.outdoor
        direction = (o.wind_direction_deg + self.noise(5.0 * noise_k)) % 360.0
        return {
            "temperature_c": round(o.temperature_c + self.noise(0.2 * noise_k), 1),
            "humidity_pct": round(clamp(o.humidity_pct + self.noise(1.5 * noise_k), 0, 100), 1),
            "illuminance_lux": round(max(0.0, o.illuminance_lux * (1 + self.noise(0.02 * noise_k)))),
            "wind_speed_ms": round(max(0.0, o.wind_speed_ms + self.noise(0.3 * noise_k)), 1),
            "wind_direction_deg": round(direction),
            "wind_direction": _compass(direction),
            "pressure_hpa": round(o.pressure_hpa + self.noise(0.3 * noise_k), 1),
            "precipitation_mm_h": round(max(0.0, o.precipitation_mm_h), 1),
            "data_available": True,
        }


# ---------------------------------------------------------------------------
# 4.1 Сейсмодатчики
# ---------------------------------------------------------------------------

@register_node_type("seysmo")
class SeismicSensor(SensorNode):
    """
    Амплитуда — пиковая скорость колебаний грунта, мм/с. Землетрясение
    фиксируется, когда амплитуда превышает порог threshold_mm_s.
    Расстояние до эпицентра оценивается (с погрешностью) по разнице
    вступлений P/S-волн — только во время события.
    """
    title = "Сейсмодатчики"
    system = "Внешние системы"
    category = "external"

    def __init__(self, node_id: str, threshold_mm_s: float = 0.5, glitch_rate_per_hour: float = 0.005,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, glitch_rate_per_hour, seed)
        self.threshold_mm_s = threshold_mm_s

    def initial_state(self) -> dict[str, Any]:
        return {
            "quake_detected": False,
            "epicenter_distance_km": None,
            "amplitude_mm_s": 0.0,
            "estimated_magnitude": None,
            "threshold_mm_s": self.threshold_mm_s,
        }

    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> dict[str, Any]:
        e = self.env.external
        amplitude = max(0.0, e.seismic_amplitude_mm_s * (1.0 + self.noise(0.1 * noise_k)))
        detected = amplitude >= node["threshold_mm_s"]
        u: dict[str, Any] = {"amplitude_mm_s": round(amplitude, 4), "quake_detected": detected}
        if detected:
            if e.quake_active:
                u["epicenter_distance_km"] = round(e.quake_distance_km * self.rng.uniform(0.85, 1.15), 1)
                u["estimated_magnitude"] = round(e.quake_magnitude + self.noise(0.2), 1)
            if not node["quake_detected"]:
                self.emit("seismic.quake_detected", Severity.WARNING, amplitude_mm_s=round(amplitude, 3),
                          distance_km=u.get("epicenter_distance_km"))
        return u

    @control("set_threshold")
    def _set_threshold(self, node, value):
        return {"threshold_mm_s": parse_number(value, 0.01, 1000.0, "Порог, мм/с")}


# ---------------------------------------------------------------------------
# 6.2 / 6.3 Датчики радиации и химии
# ---------------------------------------------------------------------------

class _AlarmSensor(SensorNode):
    """Датчик с двумя порогами (WARNING / ALARM) и фиксацией тревоги до сброса."""

    unit_key: str = ""

    def __init__(self, node_id: str, warning_threshold: float, alarm_threshold: float,
                 glitch_rate_per_hour: float = 0.01, seed: Optional[int] = None) -> None:
        super().__init__(node_id, glitch_rate_per_hour, seed)
        self.warning_threshold = warning_threshold
        self.alarm_threshold = alarm_threshold

    def _alarm_state(self, value: float, node: dict[str, Any]) -> str:
        level = "NORMAL"
        if value >= node["alarm_threshold"]:
            level = "ALARM"
        elif value >= node["warning_threshold"]:
            level = "WARNING"
        order = {"NORMAL": 0, "WARNING": 1, "ALARM": 2}
        current = node["alarm_state"]
        if order[level] > order[current]:
            self.emit(f"{self.code_name}.{level.lower()}",
                      Severity.CRITICAL if level == "ALARM" else Severity.WARNING,
                      **{self.unit_key: round(value, 3)})
            return level
        return current  # тревога держится до reset_alarm

    @control("reset_alarm")
    def _reset_alarm(self, node, value):
        return {"alarm_state": "NORMAL"}

    @control("set_thresholds")
    def _set_thresholds(self, node, value):
        """value: {"warning": x, "alarm": y}; можно передать один из порогов."""
        if not isinstance(value, dict) or not ({"warning", "alarm"} & value.keys()):
            raise ControlError('set_thresholds: ожидалось {"warning": x, "alarm": y}')
        warning = parse_number(value.get("warning", node["warning_threshold"]), 0.0, 1e6, "Порог WARNING")
        alarm = parse_number(value.get("alarm", node["alarm_threshold"]), 0.0, 1e6, "Порог ALARM")
        if warning >= alarm:
            raise ControlError("Порог WARNING должен быть меньше порога ALARM")
        return {"warning_threshold": warning, "alarm_threshold": alarm}


@register_node_type("radiation_sensor")
class RadiationSensor(_AlarmSensor):
    """Счётчик Гейгера: пуассоновская статистика отсчётов даёт шум ~1/sqrt(N)."""
    title = "Датчик радиации"
    system = "Внешние системы"
    category = "external"
    unit_key = "dose_rate_usv_h"

    CPM_PER_USV_H = 150.0  # чувствительность трубки (типично для SBM-20)

    def __init__(self, node_id: str, warning_threshold: float = 0.3, alarm_threshold: float = 1.0,
                 glitch_rate_per_hour: float = 0.005, seed: Optional[int] = None) -> None:
        super().__init__(node_id, warning_threshold, alarm_threshold, glitch_rate_per_hour, seed)

    def initial_state(self) -> dict[str, Any]:
        return {
            "dose_rate_usv_h": 0.12,
            "counts_per_minute": 18,
            "event_type": "BACKGROUND",    # BACKGROUND | ELEVATED | SPIKE
            "alarm_state": "NORMAL",       # NORMAL | WARNING | ALARM
            "warning_threshold": self.warning_threshold,
            "alarm_threshold": self.alarm_threshold,
        }

    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> dict[str, Any]:
        e = self.env.external
        expected_cpm = e.radiation_usv_h * self.CPM_PER_USV_H
        cpm = max(0, round(self.rng.gauss(expected_cpm, max(1.0, expected_cpm) ** 0.5 * noise_k)))
        dose = cpm / self.CPM_PER_USV_H
        if e.radiation_event != "BACKGROUND":
            event = e.radiation_event
        elif dose > 3 * max(e.radiation_usv_h, 0.05):
            event = "SPIKE"
        else:
            event = "BACKGROUND"
        return {"dose_rate_usv_h": round(dose, 3), "counts_per_minute": cpm, "event_type": event,
                "alarm_state": self._alarm_state(dose, node)}


@register_node_type("chem_sensor")
class ChemSensor(_AlarmSensor):
    title = "Химический датчик"
    system = "Внешние системы"
    category = "external"
    unit_key = "concentration_ppm"

    def __init__(self, node_id: str, warning_threshold: float = 5.0, alarm_threshold: float = 20.0,
                 glitch_rate_per_hour: float = 0.01, seed: Optional[int] = None) -> None:
        super().__init__(node_id, warning_threshold, alarm_threshold, glitch_rate_per_hour, seed)

    def initial_state(self) -> dict[str, Any]:
        return {
            "concentration_ppm": 0.0,
            "substance": "NONE",           # NONE | NH3 | CL2 | H2S | SO2 | UNKNOWN
            "alarm_state": "NORMAL",
            "warning_threshold": self.warning_threshold,
            "alarm_threshold": self.alarm_threshold,
        }

    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> dict[str, Any]:
        e = self.env.external
        concentration = max(0.0, e.chem_ppm + self.noise(0.05 * noise_k))
        # Слабый сигнал не идентифицируется — тип вещества определяется выше 1 ppm
        substance = e.chem_substance if concentration > 1.0 and e.chem_substance != "NONE" else \
            ("UNKNOWN" if concentration > 1.0 else "NONE")
        return {"concentration_ppm": round(concentration, 3), "substance": substance,
                "alarm_state": self._alarm_state(concentration, node)}

# dome_simulator/environment.py
"""
Модель окружения купола — общий «источник правды» для генерации данных.

Узлы песочницы — это в основном датчики и устройства, которые МЕРЯЮТ или
ИСПОЛЬЗУЮТ одни и те же физические величины (погода снаружи, воздух внутри,
фон радиации, сейсмика). Чтобы показания разных узлов были согласованы
между собой (метеостанция и солнечные панели видят одно и то же солнце,
три датчика климата — один и тот же воздух), истинные значения живут здесь,
а узлы добавляют к ним собственный шум, задержку, дрейф и отказы.

Окружение участникам напрямую не показывается (его нет в StateStore) —
они видят его только через показания датчиков.

Игровое время:
    Сутки купола длятся day_length_s секунд симуляции (по умолчанию 1440 с,
    т.е. 1 с симуляции = 1 игровая минута). Все физические процессы узлов
    (расход топлива, заряд АКБ, печать детали) считаются в ИГРОВЫХ секундах:
    game_dt = dt * time_factor. Так суточный цикл и длительность процессов
    остаются правдоподобными относительно друг друга.

Правила зависимостей и кризисы (следующий этап) могут менять поля окружения
напрямую, например `env.indoor.co2_ppm += 500` или `env.external.chem_ppm = 30`.
"""
from __future__ import annotations

import math
import random
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SECONDS_PER_DAY = 86400.0


# ---------------------------------------------------------------------------
# Вспомогательные функции генерации (используются и узлами)
# ---------------------------------------------------------------------------

def relax(value: float, target: float, tau_s: float, dt_s: float) -> float:
    """Экспоненциальное приближение value -> target с постоянной времени tau_s.
    Устойчиво при любом шаге (в отличие от value += k*(target-value)*dt)."""
    if tau_s <= 0 or dt_s <= 0:
        return target if tau_s <= 0 else value
    return target + (value - target) * math.exp(-dt_s / tau_s)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class OUProcess:
    """
    Процесс Орнштейна–Уленбека: случайное блуждание, возвращающееся к среднему.
    Даёт «живой» шум с памятью (порывы ветра, облачность, фон радиации),
    в отличие от белого шума, который скачет от тика к тику.
    """

    def __init__(self, mean: float, sigma: float, tau_s: float,
                 rng: Optional[random.Random] = None, start: Optional[float] = None) -> None:
        self.mean = mean
        self.sigma = sigma      # стационарное СКО
        self.tau_s = tau_s      # время корреляции
        self.rng = rng or random.Random()
        self.value = mean if start is None else start

    def step(self, dt_s: float) -> float:
        if dt_s <= 0:
            return self.value
        a = math.exp(-dt_s / self.tau_s)
        self.value = self.mean + (self.value - self.mean) * a \
            + self.sigma * math.sqrt(1.0 - a * a) * self.rng.gauss(0.0, 1.0)
        return self.value


def poisson_happens(rate_per_hour: float, dt_s: float, rng: random.Random) -> bool:
    """Случилось ли редкое событие с заданной частотой (раз/игровой час) за шаг dt_s."""
    if rate_per_hour <= 0 or dt_s <= 0:
        return False
    return rng.random() < 1.0 - math.exp(-rate_per_hour * dt_s / 3600.0)


def dew_point_c(temp_c: float, humidity_pct: float) -> float:
    """Точка росы по формуле Магнуса."""
    a, b = 17.62, 243.12
    rh = clamp(humidity_pct, 1.0, 100.0) / 100.0
    gamma = math.log(rh) + a * temp_c / (b + temp_c)
    return b * gamma / (a - gamma)


# ---------------------------------------------------------------------------
# Состояние окружения
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class OutdoorWeather:
    hour: float = 8.0                 # игровое время суток, 0..24
    day: int = 0                      # номер игровых суток
    sun_elevation: float = 0.0        # 0..1 (синус высоты солнца, 0 ночью)
    cloud_cover: float = 0.3          # 0..1
    irradiance_w_m2: float = 0.0      # солнечная радиация на поверхности
    illuminance_lux: float = 0.0
    temperature_c: float = 12.0
    humidity_pct: float = 65.0
    pressure_hpa: float = 1013.0
    wind_speed_ms: float = 4.0
    wind_direction_deg: float = 225.0
    precipitation_mm_h: float = 0.0
    dust_level: float = 0.1           # 0..1, пыль/взвесь в воздухе (засоряет фильтры)


@dataclass(slots=True)
class IndoorAir:
    temperature_c: float = 21.0
    humidity_pct: float = 45.0
    co2_ppm: float = 650.0
    co_ppm: float = 0.5
    voc_index: float = 100.0          # индекс VOC (шкала Sensirion 1..500, 100 = норма)
    smoke_density: float = 0.0        # 0..1, оптическая плотность дыма
    occupancy: int = 6                # людей внутри купола


@dataclass(slots=True)
class ExternalHazards:
    radiation_usv_h: float = 0.12     # мощность дозы, мкЗв/ч
    radiation_event: str = "BACKGROUND"
    chem_ppm: float = 0.0             # концентрация опасного вещества снаружи
    chem_substance: str = "NONE"
    seismic_amplitude_mm_s: float = 0.02   # пиковая скорость колебаний грунта у купола
    quake_active: bool = False
    quake_magnitude: float = 0.0
    quake_distance_km: float = 0.0
    grid_available: bool = True       # внешняя электросеть (для инвертора)
    grid_voltage_v: float = 230.0


@dataclass
class Environment:
    """Контейнер окружения + его генератор. Шагается SimulatorThread ПЕРЕД тиком узлов."""

    day_length_s: float = 1440.0
    start_hour: float = 8.0
    seed: Optional[int] = None

    outdoor: OutdoorWeather = field(default_factory=OutdoorWeather)
    indoor: IndoorAir = field(default_factory=IndoorAir)
    external: ExternalHazards = field(default_factory=ExternalHazards)

    def __post_init__(self) -> None:
        if self.day_length_s <= 0:
            raise ValueError("day_length_s должен быть > 0")
        self.rng = random.Random(self.seed)
        self._lock = threading.RLock()
        self.game_time_s: float = self.start_hour * 3600.0
        self.outdoor.hour = self.start_hour

        r = self.rng
        self._clouds = OUProcess(0.35, 0.25, 3 * 3600, r, start=0.3)
        self._wind = OUProcess(4.5, 2.5, 2 * 3600, r, start=4.0)
        self._gust = OUProcess(0.0, 1.0, 120, r)
        self._pressure = OUProcess(1013.0, 6.0, 24 * 3600, r, start=1013.0)
        self._temp_weather = OUProcess(0.0, 3.0, 12 * 3600, r)
        self._humid_weather = OUProcess(0.0, 8.0, 6 * 3600, r)
        self._dust = OUProcess(0.12, 0.08, 4 * 3600, r)
        self._radiation = OUProcess(0.12, 0.01, 1800, r)
        self._voc = OUProcess(0.0, 12.0, 1800, r)
        self._co = OUProcess(0.0, 0.2, 1200, r)

        self._quake_remaining_s = 0.0
        self._quake_peak = 0.0
        self._grid_outage_remaining_s = 0.0

        self.quake_rate_per_hour = 0.02        # слабые далёкие толчки — примерно раз в 2 игровых дня
        self.grid_outage_rate_per_hour = 0.01

    # ---------------- время ----------------

    @property
    def time_factor(self) -> float:
        """Сколько игровых секунд приходится на 1 секунду симуляции."""
        return SECONDS_PER_DAY / self.day_length_s

    def game_dt(self, dt: float) -> float:
        return dt * self.time_factor

    # ---------------- шаг ----------------

    def step(self, dt: float) -> None:
        if dt <= 0:
            return
        gdt = self.game_dt(dt)
        with self._lock:
            self.game_time_s += gdt
            self._step_outdoor(gdt)
            self._step_hazards(gdt)
            self._step_indoor(gdt)

    def _step_outdoor(self, gdt: float) -> None:
        o = self.outdoor
        o.day = int(self.game_time_s // SECONDS_PER_DAY)
        o.hour = (self.game_time_s % SECONDS_PER_DAY) / 3600.0

        # Солнце: восход 6:00, закат 20:00
        o.sun_elevation = max(0.0, math.sin(math.pi * (o.hour - 6.0) / 14.0)) if 6.0 <= o.hour <= 20.0 else 0.0

        o.cloud_cover = clamp(self._clouds.step(gdt), 0.0, 1.0)
        clear_sky = 1000.0 * o.sun_elevation ** 1.2
        o.irradiance_w_m2 = clear_sky * (1.0 - 0.75 * o.cloud_cover ** 1.5)
        o.illuminance_lux = o.irradiance_w_m2 * 110.0 + (5.0 if o.sun_elevation > 0 else 0.2)

        # Температура: минимум перед рассветом, максимум ~15:00
        diurnal = 6.0 * math.sin(2 * math.pi * (o.hour - 9.0) / 24.0)
        o.temperature_c = 12.0 + diurnal + self._temp_weather.step(gdt) - 2.0 * o.cloud_cover * o.sun_elevation

        o.pressure_hpa = self._pressure.step(gdt)

        # Влажность обратна температуре; облачно и дождливо — выше
        o.humidity_pct = clamp(70.0 - 2.2 * diurnal + 15.0 * o.cloud_cover + self._humid_weather.step(gdt), 15.0, 100.0)

        o.wind_speed_ms = max(0.0, self._wind.step(gdt) + max(0.0, self._gust.step(gdt)) * (0.5 + o.wind_speed_ms / 10.0))
        o.wind_direction_deg = (o.wind_direction_deg + self.rng.gauss(0.0, 8.0) * math.sqrt(gdt / 600.0)) % 360.0

        # Осадки: только при плотной облачности и низком давлении
        rain_drive = (o.cloud_cover - 0.7) * 10.0 + (1008.0 - o.pressure_hpa) * 0.3
        o.precipitation_mm_h = round(max(0.0, rain_drive) * self.rng.uniform(0.6, 1.4), 2)

        o.dust_level = clamp(self._dust.step(gdt) + 0.02 * max(0.0, o.wind_speed_ms - 8.0)
                             - 0.05 * min(o.precipitation_mm_h, 2.0), 0.0, 1.0)

    def _step_hazards(self, gdt: float) -> None:
        e = self.external
        e.radiation_usv_h = max(0.05, self._radiation.step(gdt))
        if e.radiation_event == "BACKGROUND" and e.radiation_usv_h > 0.3:
            e.radiation_event = "ELEVATED"

        # Химия: снаружи в норме ≈ 0 (кризисы позже впрыскивают сюда значения)
        if e.chem_substance == "NONE":
            e.chem_ppm = max(0.0, self.rng.gauss(0.02, 0.02))

        # Сейсмика: микросейсмический фон + редкие слабые толчки
        background = 0.02 + 0.004 * self.outdoor.wind_speed_ms
        if self._quake_remaining_s <= 0 and poisson_happens(self.quake_rate_per_hour, gdt, self.rng):
            e.quake_magnitude = round(self.rng.uniform(2.0, 4.5), 1)
            e.quake_distance_km = round(self.rng.uniform(20.0, 400.0), 1)
            self._quake_peak = 10 ** (e.quake_magnitude - 1.5) / max(e.quake_distance_km, 1.0) * 5.0
            self._quake_remaining_s = self.rng.uniform(60.0, 240.0)
        if self._quake_remaining_s > 0:
            self._quake_remaining_s -= gdt
            e.quake_active = self._quake_remaining_s > 0
            e.seismic_amplitude_mm_s = background + self._quake_peak * self.rng.uniform(0.5, 1.0) \
                if e.quake_active else background
        else:
            e.quake_active = False
            e.seismic_amplitude_mm_s = background * self.rng.uniform(0.7, 1.3)

        # Внешняя сеть: редкие отключения на 10–90 игровых минут
        if self._grid_outage_remaining_s > 0:
            self._grid_outage_remaining_s -= gdt
            e.grid_available = self._grid_outage_remaining_s <= 0
        elif poisson_happens(self.grid_outage_rate_per_hour, gdt, self.rng):
            self._grid_outage_remaining_s = self.rng.uniform(600.0, 5400.0)
            e.grid_available = False
        e.grid_voltage_v = self.rng.gauss(230.0, 1.5) if e.grid_available else 0.0

    def _step_indoor(self, gdt: float) -> None:
        i, o = self.indoor, self.outdoor
        h = o.hour
        # Население купола: ночью все внутри и спят, днём часть на выходах
        i.occupancy = 8 if (h < 7 or h >= 21) else (5 if 9 <= h < 18 else 7)
        activity = 0.6 if (h < 7 or h >= 22) else 1.0

        # Температура: климат-система держит ~21 °C, через оболочку «протекает» наружная
        target_t = 0.85 * 21.0 + 0.15 * o.temperature_c + 0.15 * i.occupancy * activity
        i.temperature_c = relax(i.temperature_c, target_t, 3 * 3600, gdt)

        target_h = clamp(40.0 + 0.15 * (o.humidity_pct - 60.0) + 0.8 * i.occupancy, 25.0, 80.0)
        i.humidity_pct = relax(i.humidity_pct, target_h, 2 * 3600, gdt)

        # CO2: генерация людьми, базовый воздухообмен с наружным воздухом (420 ppm)
        exchange_tau = 1.5 * 3600
        equilibrium = 420.0 + i.occupancy * activity * 90.0
        i.co2_ppm = relax(i.co2_ppm, equilibrium, exchange_tau, gdt)

        i.co_ppm = max(0.0, 0.4 + self._co.step(gdt))
        work = 40.0 if 9 <= h < 19 else 0.0   # мастерская: пайка, печать, клей
        i.voc_index = clamp(90.0 + work + self._voc.step(gdt), 1.0, 500.0)
        i.smoke_density = relax(i.smoke_density, 0.0, 900, gdt)

    # ---------------- чтение ----------------

    def snapshot(self) -> dict[str, Any]:
        """Истинные значения окружения (для отладки/админки, не для участников)."""
        with self._lock:
            return {
                "game_time_s": round(self.game_time_s, 1),
                "time_factor": self.time_factor,
                "outdoor": asdict(self.outdoor),
                "indoor": asdict(self.indoor),
                "external": asdict(self.external),
            }

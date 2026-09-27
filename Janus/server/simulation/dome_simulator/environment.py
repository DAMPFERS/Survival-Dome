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

Время — гибридная модель (dome_crises.md, п. 3.1, доработка Д1):
    - суточный цикл (солнце, погода, население купола) идёт в ИГРОВОМ времени:
      сутки длятся day_length_s секунд симуляции (по умолчанию 10800 — 3 часа,
      игровой час = 7,5 минуты); game_dt = dt * time_factor;
    - физика устройств (заряд АКБ, нагрев, печать, расход) и воздух купола
      идут в реальном времени 1:1: device_dt = dt * device_time_factor.
    device_time_factor=None — прежний режим: всё в игровом времени
    (device_dt == game_dt), используется юнит-тестами узлов.

Зоны купола (Д2): FABLAB — основной объём купола, где работает Оператор и стоят
все датчики климата (env.indoor), STORAGE — склад, LIVING — жилая зона. У зон
свои дым, CO, VOC и температура.

Кризисы и правила меняют окружение через:
    force(key, value) / unforce(key)   — подменить величину (например,
                                          "outdoor.cloud_cover" или "external.grid_available");
    offset(key, delta) / clear_offset — сдвинуть величину относительно естественной;
    drivers                            — входы физики воздуха (приток, утечки,
                                          пайка, вытяжка, очаги пожара), их выставляют правила.
"""
from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SECONDS_PER_DAY = 86400.0
OUTDOOR_CO2_PPM = 420.0


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
    """Случилось ли редкое событие с заданной частотой (раз/час) за шаг dt_s."""
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
    dust_level: float = 0.1           # 0..1, пыль/взвесь в воздухе (засоряет фильтры и панели)


@dataclass(slots=True)
class IndoorAir:
    """Воздух основного объёма купола (зона FABLAB)."""
    temperature_c: float = 21.0
    humidity_pct: float = 45.0
    co2_ppm: float = 750.0
    co_ppm: float = 0.5
    voc_index: float = 100.0          # индекс VOC (шкала Sensirion 1..500, 100 = норма)
    smoke_density: float = 0.0        # 0..1, оптическая плотность дыма
    chem_ppm: float = 0.0             # опасное вещество, попавшее внутрь (Д2)
    occupancy: int = 6                # людей внутри купола
    air_exchange_m3_h: float = 200.0  # фактический воздухообмен с улицей (для отладки)


@dataclass(slots=True)
class ZoneAir:
    """Воздух отдельной зоны купола (склад, жилая зона)."""
    temperature_c: float = 20.0
    co_ppm: float = 0.4
    voc_index: float = 95.0
    smoke_density: float = 0.0


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
    grid_unstable: bool = False       # провалы/колебания сети (кризис №10)
    radio_noise_db: float = 0.0       # добавочный шум эфира (солнечная вспышка, К9)
    solar_flare: bool = False         # геомагнитная буря: спутниковый канал недоступен


@dataclass(slots=True)
class AirDrivers:
    """Входы физики воздуха купола. Выставляются правилами зависимостей каждый тик."""
    fresh_air_m3_h: float = 180.0      # наружный воздух от приточной вентиляции
    infiltration_m3_h: float = 22.0    # неплотности оболочки + открытые клапаны
    solder_active: bool = False        # идёт пайка (линия 1 «Производственный отдел»)
    fume_extraction_on: bool = True    # вытяжка FabLab работает
    printing: bool = False             # принтер печатает (пластик → VOC)
    storage_door_open: bool = False    # дверь склада не заперта
    suppression_zones: tuple = ()      # зоны, где работает пожаротушение
    fire_intensity: dict = field(default_factory=dict)   # зона -> 0..1 (очаг пожара)
    water_turbidity_k: float = 1.0     # множитель мутности источника воды (К6)


ZONES = ("FABLAB", "STORAGE", "LIVING")


@dataclass
class Environment:
    """Контейнер окружения + его генератор. Шагается SimulatorThread ПЕРЕД тиком узлов."""

    day_length_s: float = 10800.0
    start_hour: float = 8.0
    seed: Optional[int] = None
    device_time_factor: Optional[float] = 1.0   # None — физика устройств в игровом времени

    outdoor: OutdoorWeather = field(default_factory=OutdoorWeather)
    indoor: IndoorAir = field(default_factory=IndoorAir)
    external: ExternalHazards = field(default_factory=ExternalHazards)

    DOME_VOLUME_M3 = 55.0                  # эффективный объём основного воздуха купола
    CO2_PER_PERSON_M3_H = 0.011            # выдыхаемый CO2 с учётом активности
    BASE_LEAK_M3_H = 3.0

    def __post_init__(self) -> None:
        if self.day_length_s <= 0:
            raise ValueError("day_length_s должен быть > 0")
        self._lock = threading.RLock()
        self.epoch0 = time.time()
        self._init_state(self.seed, self.start_hour)

    def _init_state(self, seed: Optional[int], start_hour: float) -> None:
        self.seed = seed
        self.start_hour = start_hour
        self.rng = random.Random(seed)
        self.game_time_s: float = start_hour * 3600.0
        self.sim_time_s: float = 0.0
        self.outdoor = OutdoorWeather(hour=start_hour)
        self.indoor = IndoorAir()
        self.external = ExternalHazards()
        self.zones: dict[str, ZoneAir] = {"STORAGE": ZoneAir(), "LIVING": ZoneAir(temperature_c=21.0)}
        self.drivers = AirDrivers()
        self.forced: dict[str, Any] = {}
        self.offsets: dict[str, float] = {}

        r = self.rng
        self._clouds = OUProcess(0.35, 0.25, 3 * 3600, r, start=0.3)
        self._wind = OUProcess(4.5, 2.5, 2 * 3600, r, start=4.0)
        self._gust = OUProcess(0.0, 1.0, 120, r)
        self._pressure = OUProcess(1013.0, 6.0, 24 * 3600, r, start=1013.0)
        self._temp_weather = OUProcess(0.0, 3.0, 12 * 3600, r)
        self._humid_weather = OUProcess(0.0, 8.0, 6 * 3600, r)
        self._dust = OUProcess(0.12, 0.08, 4 * 3600, r)
        self._radiation = OUProcess(0.12, 0.01, 1800, r)
        self._voc = OUProcess(0.0, 6.0, 900, r)
        self._co = OUProcess(0.0, 0.1, 900, r)

        self._quake_remaining_s = 0.0
        self._quake_peak = 0.0
        self._grid_outage_remaining_s = 0.0

        self.quake_rate_per_hour = 0.02        # слабые далёкие толчки — примерно раз в 2 игровых дня
        self.grid_outage_rate_per_hour = 0.01

    def reset(self, seed: Optional[int] = None, start_hour: Optional[float] = None) -> None:
        """Возвращает окружение к началу (одинаковый старт смены для всех команд)."""
        with self._lock:
            self._init_state(seed if seed is not None else self.seed,
                             self.start_hour if start_hour is None else start_hour)

    # ---------------- время ----------------

    @property
    def time_factor(self) -> float:
        """Сколько игровых секунд приходится на 1 секунду симуляции."""
        return SECONDS_PER_DAY / self.day_length_s

    @property
    def device_factor(self) -> float:
        return self.time_factor if self.device_time_factor is None else self.device_time_factor

    def game_dt(self, dt: float) -> float:
        return dt * self.time_factor

    def device_dt(self, dt: float) -> float:
        """Шаг физики устройств (секунды «реального» времени устройства)."""
        return dt * self.device_factor

    def now(self) -> float:
        """Часы симуляции (unix-время), по ним ставятся метки updated_at и событий."""
        return self.epoch0 + self.sim_time_s

    # ---------------- подмены (кризисы) ----------------

    def force(self, key: str, value: Any) -> None:
        with self._lock:
            self.forced[key] = value
            self._apply_one(key)

    def unforce(self, key: str) -> None:
        with self._lock:
            self.forced.pop(key, None)

    def offset(self, key: str, delta: float) -> None:
        with self._lock:
            self.offsets[key] = delta

    def clear_offset(self, key: str) -> None:
        with self._lock:
            self.offsets.pop(key, None)

    def _target(self, key: str) -> tuple[Any, str]:
        group, attr = key.split(".", 1)
        obj = {"outdoor": self.outdoor, "indoor": self.indoor, "external": self.external,
               "drivers": self.drivers}.get(group)
        if obj is None:
            obj = self.zones[group.upper()]
        return obj, attr

    def _apply_one(self, key: str) -> None:
        obj, attr = self._target(key)
        setattr(obj, attr, self.forced[key])

    def _value(self, key: str, natural: Any) -> Any:
        """Естественное значение с учётом подмены и смещения."""
        value = self.forced.get(key, natural)
        if key in self.offsets:
            value = value + self.offsets[key]
        return value

    def zone_air(self, zone: str) -> Any:
        zone = (zone or "FABLAB").upper()
        if zone in ("FABLAB", "MAIN"):
            return self.indoor
        return self.zones.get(zone, self.indoor)

    # ---------------- шаг ----------------

    def step(self, dt: float) -> None:
        if dt <= 0:
            return
        gdt = self.game_dt(dt)
        ddt = self.device_dt(dt)
        with self._lock:
            self.sim_time_s += dt
            self.game_time_s += gdt
            self._step_outdoor(gdt)
            self._step_hazards(gdt)
            self._step_indoor(gdt, ddt)

    def _step_outdoor(self, gdt: float) -> None:
        o = self.outdoor
        o.day = int(self.game_time_s // SECONDS_PER_DAY)
        o.hour = (self.game_time_s % SECONDS_PER_DAY) / 3600.0

        # Солнце: восход 6:00, закат 20:00
        o.sun_elevation = max(0.0, math.sin(math.pi * (o.hour - 6.0) / 14.0)) if 6.0 <= o.hour <= 20.0 else 0.0

        o.cloud_cover = clamp(self._value("outdoor.cloud_cover", self._clouds.step(gdt)), 0.0, 1.0)
        clear_sky = 1000.0 * o.sun_elevation ** 1.2
        o.irradiance_w_m2 = clear_sky * (1.0 - 0.75 * o.cloud_cover ** 1.5)
        o.illuminance_lux = o.irradiance_w_m2 * 110.0 + (5.0 if o.sun_elevation > 0 else 0.2)

        # Температура: минимум перед рассветом, максимум ~15:00
        diurnal = 6.0 * math.sin(2 * math.pi * (o.hour - 9.0) / 24.0)
        natural_t = 12.0 + diurnal + self._temp_weather.step(gdt) - 2.0 * o.cloud_cover * o.sun_elevation
        o.temperature_c = self._value("outdoor.temperature_c", natural_t)

        o.pressure_hpa = self._value("outdoor.pressure_hpa", self._pressure.step(gdt))

        # Влажность обратна температуре; облачно и дождливо — выше
        natural_h = 70.0 - 2.2 * diurnal + 15.0 * o.cloud_cover + self._humid_weather.step(gdt)
        o.humidity_pct = clamp(self._value("outdoor.humidity_pct", natural_h), 15.0, 100.0)

        natural_wind = max(0.0, self._wind.step(gdt) + max(0.0, self._gust.step(gdt)) * (0.5 + o.wind_speed_ms / 10.0))
        o.wind_speed_ms = max(0.0, self._value("outdoor.wind_speed_ms", natural_wind))
        natural_dir = (o.wind_direction_deg + self.rng.gauss(0.0, 8.0) * math.sqrt(gdt / 600.0)) % 360.0
        o.wind_direction_deg = self._value("outdoor.wind_direction_deg", natural_dir) % 360.0

        # Осадки: только при плотной облачности и низком давлении
        rain_drive = (o.cloud_cover - 0.7) * 10.0 + (1008.0 - o.pressure_hpa) * 0.3
        o.precipitation_mm_h = round(self._value("outdoor.precipitation_mm_h",
                                                  max(0.0, rain_drive) * self.rng.uniform(0.6, 1.4)), 2)

        natural_dust = self._dust.step(gdt) + 0.02 * max(0.0, o.wind_speed_ms - 8.0) \
            - 0.05 * min(o.precipitation_mm_h, 2.0)
        o.dust_level = clamp(self._value("outdoor.dust_level", natural_dust), 0.0, 1.0)

    def _step_hazards(self, gdt: float) -> None:
        e = self.external
        e.radiation_usv_h = max(0.05, self._value("external.radiation_usv_h", self._radiation.step(gdt)))
        if e.radiation_event == "BACKGROUND" and e.radiation_usv_h > 0.3:
            e.radiation_event = "ELEVATED"
        elif e.radiation_event == "ELEVATED" and e.radiation_usv_h < 0.2:
            e.radiation_event = "BACKGROUND"

        # Химия: снаружи в норме ≈ 0 (кризисы подменяют external.chem_ppm)
        if "external.chem_ppm" in self.forced:
            e.chem_ppm = self.forced["external.chem_ppm"]
            e.chem_substance = self.forced.get("external.chem_substance", e.chem_substance)
        elif e.chem_substance == "NONE":
            e.chem_ppm = max(0.0, self.rng.gauss(0.02, 0.02))

        # Сейсмика: микросейсмический фон + редкие слабые толчки
        background = 0.02 + 0.004 * self.outdoor.wind_speed_ms
        if self._quake_remaining_s <= 0 and poisson_happens(self.quake_rate_per_hour, gdt, self.rng):
            self._start_quake(round(self.rng.uniform(2.0, 4.5), 1), round(self.rng.uniform(20.0, 400.0), 1),
                              self.rng.uniform(60.0, 240.0))
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
            natural_grid = self._grid_outage_remaining_s <= 0
        elif poisson_happens(self.grid_outage_rate_per_hour, gdt, self.rng):
            self._grid_outage_remaining_s = self.rng.uniform(600.0, 5400.0)
            natural_grid = False
        else:
            natural_grid = True
        e.grid_available = bool(self._value("external.grid_available", natural_grid))
        e.grid_unstable = bool(self.forced.get("external.grid_unstable", False))
        if e.grid_available:
            sigma = 6.0 if e.grid_unstable else 1.5
            e.grid_voltage_v = self.rng.gauss(230.0, sigma)
        else:
            e.grid_voltage_v = 0.0
        e.radio_noise_db = float(self.forced.get("external.radio_noise_db", 0.0))
        e.solar_flare = bool(self.forced.get("external.solar_flare", False))

    def _start_quake(self, magnitude: float, distance_km: float, duration_game_s: float) -> None:
        e = self.external
        e.quake_magnitude = magnitude
        e.quake_distance_km = distance_km
        self._quake_peak = 10 ** (magnitude - 1.5) / max(distance_km, 1.0) * 5.0
        self._quake_remaining_s = duration_game_s

    def inject_quake(self, magnitude: float, distance_km: float, duration_s: float = 20.0) -> None:
        """Землетрясение по сценарию (К10). duration_s — реальные секунды."""
        with self._lock:
            self._start_quake(magnitude, distance_km, duration_s * self.time_factor)

    # ---------------- воздух купола ----------------

    def _step_indoor(self, gdt: float, ddt: float) -> None:
        i, o, d = self.indoor, self.outdoor, self.drivers
        h = o.hour
        # Население купола: ночью все внутри и спят, днём часть на выходах
        i.occupancy = int(self._value("indoor.occupancy", 8 if (h < 7 or h >= 21) else (5 if 9 <= h < 18 else 7)))
        activity = 0.6 if (h < 7 or h >= 22) else 1.0

        # Температура и влажность следуют суточному циклу (игровое время)
        target_t = 0.85 * 21.0 + 0.15 * o.temperature_c + 0.15 * i.occupancy * activity
        # Приток наружного воздуха в мороз выстуживает купол (рекуператор КПД 75 %)
        target_t -= max(0.0, 21.0 - o.temperature_c) * 0.25 * 0.25 * min(1.0, d.fresh_air_m3_h / 300.0)
        i.temperature_c = self._value("indoor.temperature_c", relax(i.temperature_c, target_t, 3 * 3600, gdt))

        target_h = clamp(40.0 + 0.15 * (o.humidity_pct - 60.0) + 0.8 * i.occupancy, 25.0, 80.0)
        i.humidity_pct = self._value("indoor.humidity_pct", relax(i.humidity_pct, target_h, 2 * 3600, gdt))

        # Воздухообмен с улицей (м³/ч) — физика в реальном времени устройства
        q = max(0.5, d.fresh_air_m3_h + d.infiltration_m3_h + self.BASE_LEAK_M3_H)
        i.air_exchange_m3_h = round(q, 1)
        vol = self.DOME_VOLUME_M3
        tau_s = vol / q * 3600.0

        # CO2: баланс генерации людьми и воздухообмена
        gen = self.CO2_PER_PERSON_M3_H * i.occupancy * activity * (1.15 if d.solder_active else 1.0)
        co2_eq = OUTDOOR_CO2_PPM + gen * 1e6 / q
        i.co2_ppm = self._value("indoor.co2_ppm", relax(i.co2_ppm, co2_eq, tau_s, ddt))

        # Опасное вещество снаружи проникает с притоком и через неплотности
        outside_chem = self.external.chem_ppm if self.external.chem_substance != "NONE" else 0.0
        i.chem_ppm = max(0.0, relax(i.chem_ppm, outside_chem, tau_s, ddt))

        # VOC: пайка и печать — источники, вытяжка и приток — стоки
        source = 35.0 + (12.0 if d.printing else 0.0) + (340.0 if d.solder_active else 0.0)
        removal = 1.0 + (2.5 if d.fume_extraction_on else 0.0) + d.fresh_air_m3_h / 250.0
        voc_target = 90.0 + source / removal + self._voc.step(gdt)
        i.voc_index = clamp(relax(i.voc_index, voc_target, 900.0 / removal, ddt), 1.0, 500.0)

        # Зоны: склад и жилая зона
        fire = d.fire_intensity or {}
        door_k = 0.15 if d.storage_door_open else 0.02
        for name, zone in self.zones.items():
            f = clamp(float(fire.get(name, 0.0)), 0.0, 1.0)
            zone.temperature_c = relax(zone.temperature_c, i.temperature_c - 1.0 + 25.0 * f, 600, ddt)
            sprayed = name in d.suppression_zones
            smoke_target = 0.0 if sprayed and f <= 0 else 0.6 * f
            zone.smoke_density = relax(zone.smoke_density, smoke_target, 60.0 if smoke_target > zone.smoke_density
                                       else (240.0 if sprayed else 600.0), ddt)
            zone.co_ppm = relax(zone.co_ppm, 0.4 + 30.0 * f + 20.0 * zone.smoke_density, 120.0, ddt)
            zone.voc_index = clamp(relax(zone.voc_index, 95.0 + 320.0 * f, 120.0, ddt), 1.0, 500.0)

        storage = self.zones["STORAGE"]
        f_main = clamp(float(fire.get("FABLAB", 0.0)), 0.0, 1.0)
        i.co_ppm = max(0.0, self._value("indoor.co_ppm", 0.4 + self._co.step(gdt) + door_k * storage.co_ppm
                                        + 30.0 * f_main))
        main_smoke_target = max(0.6 * f_main, door_k * storage.smoke_density)
        sprayed_main = "FABLAB" in d.suppression_zones
        if sprayed_main and f_main <= 0:
            main_smoke_target = 0.0
        smoke_tau = 120.0 if main_smoke_target > i.smoke_density else (240.0 if sprayed_main else 600.0)
        i.smoke_density = self._value("indoor.smoke_density",
                                      relax(i.smoke_density, main_smoke_target, smoke_tau, ddt))
        i.voc_index = clamp(i.voc_index + door_k * max(0.0, storage.voc_index - 95.0), 1.0, 500.0)

    # ---------------- чтение ----------------

    def snapshot(self) -> dict[str, Any]:
        """Истинные значения окружения (для отладки/админки, не для участников)."""
        with self._lock:
            return {
                "game_time_s": round(self.game_time_s, 1),
                "sim_time_s": round(self.sim_time_s, 1),
                "time_factor": self.time_factor,
                "device_time_factor": self.device_factor,
                "outdoor": asdict(self.outdoor),
                "indoor": asdict(self.indoor),
                "zones": {name: asdict(z) for name, z in self.zones.items()},
                "external": asdict(self.external),
                "drivers": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self.drivers).items()},
                "forced": dict(self.forced),
                "offsets": dict(self.offsets),
            }

# seismic_core.py
"""
Физическое ядро симулятора: датчики, событие землетрясения, расчёт волновой формы.
Никаких зависимостей от Qt — можно переиспользовать/тестировать отдельно.
"""
from dataclasses import dataclass, field
from typing import Dict, Tuple
import math
import numpy as np
from scipy.signal import lfilter


@dataclass
class Sensor:
    id: str
    x: float                     # координата, км (восток)
    y: float                     # координата, км (север)
    z: float = 0.0                # глубина установки датчика, км (обычно 0)
    sampling_rate: float = 100.0  # Гц


@dataclass
class EarthquakeEvent:
    magnitude: float
    x: float
    y: float
    depth: float                 # км
    seed: int = 42


AXES = ("Z", "N", "E")


def _distance(event: EarthquakeEvent, sensor: Sensor) -> float:
    """Гипоцентральное расстояние, км."""
    return math.hypot(event.x - sensor.x, event.y - sensor.y, event.depth - sensor.z)


def compute_event_waveform(event: EarthquakeEvent, sensor: Sensor, vp: float, vs: float) -> Dict[str, dict]:
    """
    Считает полную волновую форму землетрясения для одного датчика по всем 3 осям.
    Массив ускорений индексирован от t=0 (момент возникновения события в гипоцентре),
    то есть waveform[i] соответствует моменту времени i/sampling_rate ПОСЛЕ события.

    Возвращает dict: axis -> {"acc": np.ndarray, "t_p": float, "t_s": float, "pga": float}
    """
    dist = max(_distance(event, sensor), 0.5)
    t_p = dist / vp
    t_s = dist / vs

    # с запасом на затухание после S-волны (коду волны нужно "дожить" до тишины)
    duration = t_s + 12.0
    dt = 1.0 / sensor.sampling_rate
    n_samples = int(duration * sensor.sampling_rate)
    t = np.arange(n_samples) * dt

    pga_target = 10 ** (0.5 * event.magnitude - 0.9 * math.log10(dist) - 0.8)
    f_dom = 2.5 * 10 ** (-0.3 * (event.magnitude - 3.0))

    result = {}
    for axis in AXES:
        rng = np.random.default_rng(event.seed + {"Z": 100, "N": 200, "E": 300}[axis])

        if axis in ("N", "E"):
            # горизонтальные каналы: доминирует S-волна, она сильнее
            center_t, sigma_t = t_s, 1.5 / f_dom
            amp_scale = pga_target * 1.2
            freq = f_dom * 0.8
        else:
            # вертикальный канал: заметнее P-волна, амплитуда обычно меньше
            center_t, sigma_t = t_p, 0.8 / f_dom
            amp_scale = pga_target * 0.6
            freq = f_dom * 1.2

        envelope = amp_scale * np.exp(-0.5 * ((t - center_t) / sigma_t) ** 2)
        noise = rng.standard_normal(n_samples)
        alpha = math.exp(-2 * math.pi * freq * dt)
        b, a = [1 - alpha], [1, -alpha]
        noise = lfilter(b, a, noise)

        acc = envelope * noise

        result[axis] = {
            "acc": acc.astype(np.float64),
            "t_p": t_p,
            "t_s": t_s,
            "pga": float(np.max(np.abs(acc))) if n_samples else 0.0,
            "n_samples": n_samples,
        }

    return result
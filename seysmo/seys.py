# seismic_simulator.py
from dataclasses import dataclass
from typing import List, Dict, Tuple
from pathlib import Path
import csv
import math
import numpy as np
from scipy.signal import butter, filtfilt

@dataclass
class Sensor:
    id: str
    x: float  # км (восток)
    y: float  # км (север)
    z: float = 0.0  # км (глубина, 0 = поверхность)
    sampling_rate: float = 100.0  # Гц

@dataclass
class EarthquakeEvent:
    magnitude: float  # Mw
    x: float  # км
    y: float
    depth: float  # км
    seed: int = 42

class SeismicBatchSimulator:
    """
    Упрощённый, но физически обоснованный генератор сейсмозаписей.
    Использует стохастический метод Бруна с эмпирическим масштабированием амплитуд.
    """
    def __init__(self, vp: float = 6.0, vs: float = 3.5, q: float = 150):
        self.vp = vp  # скорость P-волн, км/с
        self.vs = vs  # скорость S-волн, км/с
        self.q = q    # добротность среды (затухание)

    def _calc_distance(self, event: EarthquakeEvent, sensor: Sensor) -> float:
        return math.hypot(event.x - sensor.x, event.y - sensor.y, event.depth - sensor.z)

    def _generate_waveform(self, event: EarthquakeEvent, sensor: Sensor, axis: str) -> Tuple[np.ndarray, np.ndarray, float]:
        rng = np.random.default_rng(event.seed + {"Z": 100, "N": 200, "E": 300}[axis])
        dist = max(self._calc_distance(event, sensor), 0.5)
        
        t_p = dist / self.vp
        t_s = dist / self.vs
        duration = t_s + 8.0
        n_samples = int(duration * sensor.sampling_rate)
        dt = 1.0 / sensor.sampling_rate
        t = np.arange(n_samples) * dt

        pga_target = 10**(0.5 * event.magnitude - 0.9 * math.log10(dist) - 0.8)
        
        f_dom = 2.5 * 10**(-0.3 * (event.magnitude - 3.0))
        if axis in ('N', 'E'):
            center_t, sigma_t = t_s, 1.5 / f_dom
            amp_scale = pga_target * 1.2
            freq = f_dom * 0.8
        else:
            center_t, sigma_t = t_p, 0.8 / f_dom
            amp_scale = pga_target * 0.6
            freq = f_dom * 1.2

        envelope = amp_scale * np.exp(-0.5 * ((t - center_t) / sigma_t)**2)
        
        noise = rng.standard_normal(n_samples)
        # Быстрый экспоненциальный фильтр (заменяет ручной цикл)
        alpha = math.exp(-2 * math.pi * freq * dt)
        b, a = [1 - alpha], [1, -alpha]
        from scipy.signal import lfilter  # уже импортирована в вашем коде, можно вынести в начало файла
        noise = lfilter(b, a, noise)
            
        waveform = envelope * noise
        bg_noise = rng.normal(0, 1e-4 * pga_target, n_samples)
        waveform += bg_noise

        return waveform, t, pga_target

    def generate_batch(self, event: EarthquakeEvent, sensors: List[Sensor], 
                       output_dir: str = "./seismic_data") -> Dict[str, Dict]:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        summary = {}
        
        # Буфер для CSV (оптимизация: запись пачками)
        wave_path = Path(output_dir) / "waveforms.csv"
        with open(wave_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", "sensor_id", "axis", "acceleration_mps2", "velocity_mps"])
            
            for sensor in sensors:
                sensor_metrics = {}
                for axis in ("Z", "N", "E"):
                    acc, time_arr, pga_est = self._generate_waveform(event, sensor, axis)
                    
                    # Вычисление скорости (интегрирование + HPF для дрейфа)
                    vel = np.cumsum(acc) / sensor.sampling_rate
                    # Простой High-Pass фильтр для удаления линейного дрейфа
                    if len(acc) > 10:
                        vel -= np.convolve(vel, np.ones(10)/10, mode='same')
                    
                    pgv_val = float(np.max(np.abs(vel)))
                    
                    if axis == "Z":  # метрики берём по вертикали (стандарт для PGA/PGV в играх)
                        sensor_metrics["pga_mps2"] = float(np.max(np.abs(acc)))
                        sensor_metrics["pgv_mps"] = pgv_val

                    # Построчная запись (оптимизирована через zip)
                    for row in zip(time_arr, [sensor.id]*len(time_arr), [axis]*len(time_arr), acc, vel):
                        writer.writerow([f"{row[0]:.4f}", row[1], row[2], f"{row[3]:.6f}", f"{row[4]:.6f}"])
                
                summary[sensor.id] = sensor_metrics

        # Сохранение сводки
        with open(Path(output_dir) / "summary.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["sensor_id", "pga_mps2", "pgv_mps"])
            writer.writeheader()
            for sid, metrics in summary.items():
                writer.writerow({"sensor_id": sid, **metrics})
                
        return summary
    
    
if __name__ == "__main__":
    # 1. Конфигурация события
    eq = EarthquakeEvent(
        magnitude=5.2,
        x=12.5, y=8.0, depth=10.0,
        seed=20240529
    )

    # 2. Сеть датчиков (игровые объекты)
    sensors = [
        Sensor(id="STN_A", x=10.0, y=8.0, sampling_rate=100),
        Sensor(id="STN_B", x=15.0, y=12.0, sampling_rate=200),
        Sensor(id="STN_C", x=12.5, y=8.1, sampling_rate=50),
    ]

    # 3. Генерация
    sim = SeismicBatchSimulator(vp=6.0, vs=3.5, q=120)
    metrics = sim.generate_batch(eq, sensors, output_dir="D:/PROGRAMS/Survival-Dome/seysmo/data")

    # 4. Интеграция в игровую логику
    for sid, m in metrics.items():
        print(f"{sid}: PGA={m['pga_mps2']:.3f} м/с², PGV={m['pgv_mps']:.4f} м/с")
        if m["pga_mps2"] > 0.2:  # Порог разрушения инфраструктуры
            print(f"  ⚠️ {sid} превысил допустимую нагрузку!")
"""
locator.py — локация гипоцентра землетрясения и оценка магнитуды.

Алгоритмы:
1. Локация гипоцентра — метод наименьших квадратов (least_squares)
   по временам прихода P-волн на 3+ датчиках.
2. Оценка магнитуды — инверсия формулы из seismic_core.py.

Для студентов: используется scipy.optimize.least_squares для решения
системы нелинейных уравнений.
"""
import numpy as np
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from scipy.optimize import least_squares
import math


@dataclass
class HypocenterSolution:
    """Результат локации гипоцентра."""
    x: float  # координата X, км
    y: float  # координата Y, км
    depth: float  # глубина, км
    origin_time: float  # время возникновения события, сек
    magnitude: float  # оценка магнитуды
    residual: float  # невязка (RMS), сек


class HypocenterLocator:
    """
    Локатор гипоцентра землетрясения.
    
    Использует времена прихода P-волн на нескольких датчиках
    для определения координат и времени события.
    """
    
    def __init__(self, vp: float = 6.0):
        """
        Args:
            vp: скорость P-волны, км/с
        """
        self.vp = vp
    
    def locate(self, p_arrivals: Dict[str, float], 
           sensor_positions: Dict[str, Tuple[float, float, float]]) -> Optional[HypocenterSolution]:
        """
        Определяет гипоцентр по временам прихода P-волн.
        """
        if len(p_arrivals) < 3:
            print(f"[Locator] Недостаточно данных: {len(p_arrivals)} датчиков (нужно минимум 3)")
            return None
        
        # Подготовка данных
        sensor_ids = list(p_arrivals.keys())
        n_sensors = len(sensor_ids)
        
        # Координаты датчиков
        sensor_coords = np.array([sensor_positions[sid] for sid in sensor_ids])  # (n, 3)
        
        # Времена прихода P-волн
        arrival_times = np.array([p_arrivals[sid] for sid in sensor_ids])  # (n,)
        
        # Начальное приближение: среднее по датчикам
        x0 = np.mean(sensor_coords[:, 0])
        y0 = np.mean(sensor_coords[:, 1])
        z0 = 10.0  # начальная глубина 10 км
        t0 = np.min(arrival_times) - 5.0  # время события до первого прихода
        
        initial_guess = np.array([x0, y0, z0, t0])
        
        # Функция невязок
        def residuals(params):
            x, y, z, t_origin = params
            # Гипоцентральные расстояния
            distances = np.sqrt(
                (sensor_coords[:, 0] - x) ** 2 +
                (sensor_coords[:, 1] - y) ** 2 +
                (sensor_coords[:, 2] - z) ** 2
            )
            # Теоретические времена прихода
            theoretical_times = t_origin + distances / self.vp
            # Невязки
            return arrival_times - theoretical_times
        
        # ✅ ИСПРАВЛЕНИЕ: используем метод 'trf' (Trust Region Reflective)
        # Он работает даже когда количество наблюдений < количества параметров
        try:
            result = least_squares(
                residuals, 
                initial_guess, 
                method='trf',  # ✅ изменили с 'lm' на 'trf'
                bounds=([-np.inf, -np.inf, 0.0, -np.inf],  # нижние границы (z >= 0)
                        [np.inf, np.inf, np.inf, np.inf])   # верхние границы
            )
            
            if not result.success:
                print(f"[Locator] Оптимизация не сошлась: {result.message}")
                return None
            
            x, y, z, t_origin = result.x
            residual_rms = np.sqrt(np.mean(result.fun ** 2))
            
            return HypocenterSolution(
                x=x,
                y=y,
                depth=z,
                origin_time=t_origin,
                magnitude=0.0,  # будет вычислена отдельно
                residual=residual_rms,
            )
            
        except Exception as e:
            print(f"[Locator] Ошибка оптимизации: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def estimate_magnitude(self, hypocenter: HypocenterSolution,
                           pga_by_sensor: Dict[str, float],
                           sensor_positions: Dict[str, Tuple[float, float, float]]) -> float:
        """
        Оценивает магнитуду землетрясения по PGA и гипоцентральным расстояниям.
        
        Инверсия формулы из seismic_core.py:
            PGA = 10^(0.5*M - 0.9*log10(dist) - 0.8)
            => M = 2 * (log10(PGA) + 0.9*log10(dist) + 0.8)
        
        Args:
            hypocenter: результат локации гипоцентра
            pga_by_sensor: dict sensor_id -> PGA (м/с²)
            sensor_positions: dict sensor_id -> (x, y, z) координаты датчика (км)
        
        Returns:
            float: оценка магнитуды
        """
        magnitudes = []
        
        for sensor_id, pga in pga_by_sensor.items():
            if pga <= 0:
                continue
            
            # Гипоцентральное расстояние
            sx, sy, sz = sensor_positions[sensor_id]
            dist = math.hypot(
                hypocenter.x - sx,
                hypocenter.y - sy,
                hypocenter.depth - sz
            )
            dist = max(dist, 0.5)  # избегаем log(0)
            
            # Инверсия формулы
            mag = 2.0 * (math.log10(pga) + 0.9 * math.log10(dist) + 0.8)
            magnitudes.append(mag)
        
        if not magnitudes:
            return 0.0
        
        # Среднее по всем датчикам
        return float(np.mean(magnitudes))
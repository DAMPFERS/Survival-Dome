"""
detector.py — детектор сейсмических событий и пикинг P/S волн.

Алгоритмы:
1. STA/LTA (Short-Term Average / Long-Term Average) — детекция событий
2. Пикинг P-волны — по вертикальной оси Z (первый резкий рост)
3. Пикинг S-волны — по горизонтальным осям N/E (резкий рост после P)

Для студентов: алгоритмы реализованы прозрачно на NumPy, без использования obspy.
"""
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class EventDetection:
    """Результат детекции события на одном датчике."""
    sensor_id: str
    trigger_time: float  # время срабатывания триггера (STA/LTA > threshold)
    p_wave_time: Optional[float] = None  # время прихода P-волны
    s_wave_time: Optional[float] = None  # время прихода S-волны
    pga: float = 0.0  # пиковое ускорение (peak ground acceleration)


class EventDetector:
    """
    Детектор сейсмических событий на основе STA/LTA.
    
    Анализирует поток данных по каждому датчику отдельно.
    """
    
    def __init__(self, 
                 sta_window: float = 1.0,   # короткое окно (сек)
                 lta_window: float = 10.0,  # длинное окно (сек)
                 trigger_threshold: float = 3.5,  # порог срабатывания
                 detrack_threshold: float = 2.0,    # порог окончания события
                 sampling_rate: float = 100.0):
        """
        Args:
            sta_window: длина короткого окна для STA (сек)
            lta_window: длина длинного окна для LTA (сек)
            trigger_threshold: порог STA/LTA для срабатывания триггера
            detrack_threshold: порог STA/LTA для окончания события
            sampling_rate: частота дискретизации (Гц)
        """
        self.sta_window = sta_window
        self.lta_window = lta_window
        self.trigger_threshold = trigger_threshold
        self.detrack_threshold = detrack_threshold
        self.sampling_rate = sampling_rate
        
        self.sta_samples = int(sta_window * sampling_rate)
        self.lta_samples = int(lta_window * sampling_rate)
        
        # Состояние по каждому датчику
        self._buffers: Dict[str, Dict[str, np.ndarray]] = {}  # sensor_id -> axis -> array
        self._triggered: Dict[str, bool] = {}  # sensor_id -> is_triggered
        self._last_trigger_time: Dict[str, float] = {}  # sensor_id -> time
    
    def _ensure_buffer(self, sensor_id: str):
        """Инициализирует буфер для датчика, если нужно."""
        if sensor_id not in self._buffers:
            self._buffers[sensor_id] = {
                'Z': np.array([], dtype=np.float64),
                'N': np.array([], dtype=np.float64),
                'E': np.array([], dtype=np.float64),
            }
            self._triggered[sensor_id] = False
            self._last_trigger_time[sensor_id] = -999.0
    
    def process_chunk(self, sensor_id: str, timestamp: float,
                      z: np.ndarray, n: np.ndarray, e: np.ndarray) -> Optional[EventDetection]:
        """
        Обрабатывает чанк данных от одного датчика.
        
        Args:
            sensor_id: ID датчика
            timestamp: время первого отсчёта в чанке (сек)
            z, n, e: массивы ускорений по осям
        
        Returns:
            EventDetection: если обнаружено событие, иначе None
        """
        self._ensure_buffer(sensor_id)
        
        # Добавляем данные в буфер
        self._buffers[sensor_id]['Z'] = np.concatenate([self._buffers[sensor_id]['Z'], z])
        self._buffers[sensor_id]['N'] = np.concatenate([self._buffers[sensor_id]['N'], n])
        self._buffers[sensor_id]['E'] = np.concatenate([self._buffers[sensor_id]['E'], e])
        
        # Ограничиваем размер буфера (храним только последние 30 секунд)
        max_samples = int(30.0 * self.sampling_rate)
        for axis in ['Z', 'N', 'E']:
            if len(self._buffers[sensor_id][axis]) > max_samples:
                self._buffers[sensor_id][axis] = self._buffers[sensor_id][axis][-max_samples:]
        
        # Проверяем, достаточно ли данных для STA/LTA
        if len(self._buffers[sensor_id]['Z']) < self.lta_samples:
            return None
        
        # Вычисляем STA/LTA по вертикальной оси (для P-волны)
        sta_lta_z = self._compute_sta_lta(self._buffers[sensor_id]['Z'])
        
        # Проверяем триггер
        current_sta_lta = sta_lta_z[-1]
        current_time = timestamp + (len(z) - 1) / self.sampling_rate
        
        detection = None
        
        if not self._triggered[sensor_id]:
            # Событие не активно — проверяем срабатывание
            if current_sta_lta > self.trigger_threshold:
                self._triggered[sensor_id] = True
                self._last_trigger_time[sensor_id] = current_time
                
                # Пикинг P и S волн
                p_time = self._pick_p_wave(sensor_id, timestamp)
                s_time = self._pick_s_wave(sensor_id, timestamp, p_time)
                
                # PGA по всем осям
                pga = max(
                    np.max(np.abs(self._buffers[sensor_id]['Z'][-int(5 * self.sampling_rate):])),
                    np.max(np.abs(self._buffers[sensor_id]['N'][-int(5 * self.sampling_rate):])),
                    np.max(np.abs(self._buffers[sensor_id]['E'][-int(5 * self.sampling_rate):])),
                )
                
                detection = EventDetection(
                    sensor_id=sensor_id,
                    trigger_time=current_time,
                    p_wave_time=p_time,
                    s_wave_time=s_time,
                    pga=pga,
                )
        else:
            # Событие активно — проверяем окончание
            if current_sta_lta < self.detrack_threshold:
                self._triggered[sensor_id] = False
        
        return detection
    
    def _compute_sta_lta(self, signal: np.ndarray) -> np.ndarray:
        """
        Вычисляет отношение STA/LTA для сигнала.
        
        STA (Short-Term Average) — средняя энергия в коротком окне.
        LTA (Long-Term Average) — средняя энергия в длинном окне.
        
        Returns:
            np.ndarray: массив STA/LTA (той же длины, что и signal)
        """
        # Энергия сигнала (квадрат амплитуды)
        energy = signal ** 2
        
        # Скользящее среднее для STA и LTA
        sta = self._moving_average(energy, self.sta_samples)
        lta = self._moving_average(energy, self.lta_samples)
        
        # Избегаем деления на ноль
        lta = np.maximum(lta, 1e-10)
        
        return sta / lta
    
    def _moving_average(self, signal: np.ndarray, window: int) -> np.ndarray:
        """Вычисляет скользящее среднее с помощью cumsum (быстро)."""
        cumsum = np.cumsum(np.insert(signal, 0, 0))
        ma = (cumsum[window:] - cumsum[:-window]) / window
        # Дополняем нулями в начале
        return np.concatenate([np.zeros(window - 1), ma])
    
    def _pick_p_wave(self, sensor_id: str, chunk_start_time: float) -> Optional[float]:
        """
        Пикинг P-волны по вертикальной оси Z.
        
        Ищем первый резкий рост STA/LTA (превышение порога).
        
        Returns:
            float: время прихода P-волны (сек), или None
        """
        z_signal = self._buffers[sensor_id]['Z']
        if len(z_signal) < self.lta_samples:
            return None
        
        sta_lta = self._compute_sta_lta(z_signal)
        
        # Ищем первый момент, когда STA/LTA > trigger_threshold
        trigger_indices = np.where(sta_lta > self.trigger_threshold)[0]
        if len(trigger_indices) == 0:
            return None
        
        first_trigger_idx = trigger_indices[0]
        
        # Время относительно начала буфера
        buffer_start_time = chunk_start_time - (len(z_signal) - 1) / self.sampling_rate
        p_time = buffer_start_time + first_trigger_idx / self.sampling_rate
        
        return p_time
    
    def _pick_s_wave(self, sensor_id: str, chunk_start_time: float, 
                     p_time: Optional[float]) -> Optional[float]:
        """
        Пикинг S-волны по горизонтальным осям N/E.
        
        Ищем резкий рост энергии после P-волны (обычно через 1-3 секунды).
        
        Returns:
            float: время прихода S-волны (сек), или None
        """
        if p_time is None:
            return None
        
        # Ищем S-волну в окне [p_time + 0.5с, p_time + 10с]
        buffer_start_time = chunk_start_time - (len(self._buffers[sensor_id]['Z']) - 1) / self.sampling_rate
        
        search_start_idx = int((p_time - buffer_start_time + 0.5) * self.sampling_rate)
        search_end_idx = int((p_time - buffer_start_time + 10.0) * self.sampling_rate)
        
        search_start_idx = max(0, search_start_idx)
        search_end_idx = min(len(self._buffers[sensor_id]['N']), search_end_idx)
        
        if search_start_idx >= search_end_idx:
            return None
        
        # Горизонтальные оси
        n_signal = self._buffers[sensor_id]['N'][search_start_idx:search_end_idx]
        e_signal = self._buffers[sensor_id]['E'][search_start_idx:search_end_idx]
        
        # Энергия по горизонталам
        horiz_energy = n_signal ** 2 + e_signal ** 2
        
        # Ищем максимум энергии в окне поиска
        max_idx = np.argmax(horiz_energy)
        s_time = buffer_start_time + (search_start_idx + max_idx) / self.sampling_rate
        
        return s_time
    
    def get_sta_lta_curve(self, sensor_id: str) -> Optional[np.ndarray]:
        """
        Возвращает текущую STA/LTA кривую для визуализации (для студентов).
        
        Returns:
            np.ndarray: массив STA/LTA, или None
        """
        if sensor_id not in self._buffers:
            return None
        
        z_signal = self._buffers[sensor_id]['Z']
        if len(z_signal) < self.lta_samples:
            return None
        
        return self._compute_sta_lta(z_signal)
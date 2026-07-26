"""
detector.py — детектор сейсмических событий и пикинг P/S волн.

Алгоритмы:
1. STA/LTA (Short-Term Average / Long-Term Average) — детектирование событий
2. Пикинг P-волны — по вертикальной оси Z (первый резкий рост)
3. Пикинг S-волны — по горизонтальным осям N/E (резкий рост после P)

Для студента: алгоритм реализован без использования обсы, только с помощью NumPy.
"""
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

@dataclass
class EventDetection:
    """
    Результат детектирования события на одном датчике.
    """
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
                 sta_window: float = 1.0,   # длина короткого окна (сек)
                 lta_window: float = 10.0,  # длина длинного окна (сек)
                 trigger_threshold: float = 3.5,  # порог STA/LTA для срабатывания триггера
                 detrack_threshold: float = 2.0,    # порог STA/LTA для окончания события
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

        # Состояние буферов для каждого датчика
        self._buffers: Dict[str, Dict[str, np.ndarray]] = {}  # sensor_id -> axis -> array
        self._triggered: Dict[str, bool] = {}  # sensor_id -> is_triggered
        self._last_trigger_time: Dict[str, float] = {}  # sensor_id -> time
        # Новое: хранение абсолютного времени начала буфера
        self._buffer_start_time: Dict[str, float] = {}  # sensor_id -> absolute start time of buffer

    def _ensure_buffer(self, sensor_id: str):
        """
        Инициализирует буфер для датчика, если нужно.
        """
        if sensor_id not in self._buffers:
            self._buffers[sensor_id] = {
                'Z': np.array([], dtype=np.float64),
                'N': np.array([], dtype=np.float64),
                'E': np.array([], dtype=np.float64),
            }
            self._triggered[sensor_id] = False
            self._last_trigger_time[sensor_id] = -999.0
            self._buffer_start_time[sensor_id] = 0.0

    def process_chunk(self, sensor_id: str, timestamp: float,
                    z: np.ndarray, n: np.ndarray, e: np.ndarray,
                    chunk_length: int = None) -> Optional[EventDetection]:
        """
        Обрабатывает чанк данных от одного датчика.

        Args:
            sensor_id: ID датчика
            timestamp: абсолютное время первого отсчета в чанке (сек)
            z, n, e: массивы ускорений по осям
            chunk_length: длина чанка (число отсчетов)

        Returns:
            EventDetection: если обнаружено событие, иначе None
        """
        if chunk_length is None:
            chunk_length = len(z)

        self._ensure_buffer(sensor_id)

        # Добавляем данные в буфер
        self._buffers[sensor_id]['Z'] = np.concatenate([self._buffers[sensor_id]['Z'], z])
        self._buffers[sensor_id]['N'] = np.concatenate([self._buffers[sensor_id]['N'], n])
        self._buffers[sensor_id]['E'] = np.concatenate([self._buffers[sensor_id]['E'], e])

        # Ограничиваем размер буфера (храним только последние 30 секунд)
        max_samples = int(30.0 * self.sampling_rate)
        for axis in ['Z', 'N', 'E']:
            if len(self._buffers[sensor_id][axis]) > max_samples:
                # Обрезаем старые данные
                old_length = len(self._buffers[sensor_id][axis])
                self._buffers[sensor_id][axis] = self._buffers[sensor_id][axis][-max_samples:]
                # Корректируем время начала буфера
                self._buffer_start_time[sensor_id] += (old_length - max_samples) / self.sampling_rate

        # Проверяем, достаточно ли данных для STA/LTA
        if len(self._buffers[sensor_id]['Z']) < self.lta_samples:
            return None

        # Вычисляем STA/LTA по вертикальной оси (для детектирования P-волны)
        sta_lta_z = self._compute_sta_lta(self._buffers[sensor_id]['Z'])

        # Проверяем триггер
        current_sta_lta = sta_lta_z[-1]
        current_time = timestamp + (chunk_length - 1) / self.sampling_rate

        detection = None

        if not self._triggered[sensor_id]:
            # Событие не активно — проверяем срабатывание триггера
            if current_sta_lta > self.trigger_threshold:
                self._triggered[sensor_id] = True
                self._last_trigger_time[sensor_id] = current_time

                # Пикинг P и S волн
                p_time = self._pick_p_wave(sensor_id, timestamp, chunk_length)
                s_time = self._pick_s_wave(sensor_id, timestamp, chunk_length, p_time)

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

    def _pick_p_wave(self, sensor_id: str, chunk_start_time: float, chunk_length: int) -> Optional[float]:
        """
        Пикинг P-волны по вертикальной оси Z.
        """
        z_signal = self._buffers[sensor_id]['Z']
        if len(z_signal) < self.lta_samples:
            return None

        sta_lta = self._compute_sta_lta(z_signal)

        trigger_indices = np.where(sta_lta > self.trigger_threshold)[0]
        if len(trigger_indices) == 0:
            return None

        first_trigger_idx = trigger_indices[0]

        # Правильное вычисление времени метки
        buffer_start_time = self._buffer_start_time[sensor_id]

        p_time = buffer_start_time + first_trigger_idx / self.sampling_rate

        print(f"[Detector] P-wave picked at {p_time:.2f}s for {sensor_id}")
        return p_time

    def _pick_s_wave(self, sensor_id: str, chunk_start_time: float,
                    chunk_length: int, p_time: Optional[float]) -> Optional[float]:
        """
        Пикинг S-волны по горизонтальным осям N/E.
        """
        if p_time is None:
            return None

        z_signal = self._buffers[sensor_id]['Z']
        buffer_length = len(z_signal)
        buffer_start_time = self._buffer_start_time[sensor_id]

        search_start_idx = int((p_time - buffer_start_time + 0.5) * self.sampling_rate)
        search_end_idx = int((p_time - buffer_start_time + 10.0) * self.sampling_rate)

        search_start_idx = max(0, search_start_idx)
        search_end_idx = min(buffer_length, search_end_idx)

        if search_start_idx >= search_end_idx:
            return None

        n_signal = self._buffers[sensor_id]['N'][search_start_idx:search_end_idx]
        e_signal = self._buffers[sensor_id]['E'][search_start_idx:search_end_idx]

        horiz_energy = n_signal ** 2 + e_signal ** 2
        max_idx = np.argmax(horiz_energy)
        s_time = buffer_start_time + (search_start_idx + max_idx) / self.sampling_rate

        print(f"[Detector] S-wave picked at {s_time:.2f}s for {sensor_id}")
        return s_time

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
        """
        Вычисляет скользящее среднее с окном window.
        """
        cumsum = np.cumsum(np.insert(signal, 0, 0))
        ma = (cumsum[window:] - cumsum[:-window]) / window
        # Дополняем нулями в начале
        return np.concatenate([np.zeros(window - 1), ma])

    def get_sta_lta_curve(self, sensor_id: str) -> Optional[np.ndarray]:
        """
        Возвращает текущую кривую STA/LTA для визуализации (для отладки).

        Returns:
            np.ndarray: массив STA/LTA, или None
        """
        if sensor_id not in self._buffers:
            return None

        z_signal = self._buffers[sensor_id]['Z']
        if len(z_signal) < self.lta_samples:
            return None

        return self._compute_sta_lta(z_signal)
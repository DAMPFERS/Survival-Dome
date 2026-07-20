"""
Поток генерации данных. Не зависит от конкретного GUI-окна.
"""
import time
import math
import threading
from typing import List, Dict, Optional
import numpy as np
from scipy.signal import lfilter
from PyQt6.QtCore import QThread, pyqtSignal
from seismic_core import Sensor, EarthquakeEvent, compute_event_waveform, AXES


class SeismicDataGenerator(QThread):
    new_chunk = pyqtSignal(dict)
    event_started = pyqtSignal(dict, object)

    def __init__(self,
                 sensors: List[Sensor],
                 vp: float = 6.0,
                 vs: float = 3.5,
                 update_interval: float = 0.05,
                 background_std: float = 2.5e-5,
                 background_freq: float = 1.2,
                 parent=None):
        super().__init__(parent)
        self.sensors = sensors
        self.vp = vp
        self.vs = vs
        self.update_interval = update_interval
        self.background_std = background_std
        self.background_freq = background_freq

        self._lock = threading.Lock()
        self._noise_zi: Dict[str, Dict[str, np.ndarray]] = {}
        self._noise_rng: Dict[str, Dict[str, np.random.Generator]] = {}
        self._sample_count: Dict[str, int] = {}
        for sensor in self.sensors:
            self._noise_zi[sensor.id] = {ax: np.zeros(1) for ax in AXES}
            self._noise_rng[sensor.id] = {ax: np.random.default_rng() for ax in AXES}
            self._sample_count[sensor.id] = 0

        self._event_waveforms: Optional[Dict[str, Dict[str, dict]]] = None
        self._event_progress: Dict[str, int] = {}

    def trigger_earthquake(self, x: float, y: float, depth: float,
                           magnitude: float, seed: Optional[int] = None):
        if seed is None:
            seed = int(time.time() * 1000) % (2**31)
        event = EarthquakeEvent(magnitude=magnitude, x=x, y=y, depth=depth, seed=seed)
        waveforms: Dict[str, Dict[str, dict]] = {}
        markers: Dict[str, dict] = {}
        with self._lock:
            for sensor in self.sensors:
                wf = compute_event_waveform(event, sensor, self.vp, self.vs)
                waveforms[sensor.id] = wf
                now_abs = self._sample_count[sensor.id] / sensor.sampling_rate
                markers[sensor.id] = {
                    "t_p_abs": now_abs + wf["Z"]["t_p"],
                    "t_s_abs": now_abs + wf["Z"]["t_s"],
                    "start_abs": now_abs,
                }
            self._event_waveforms = waveforms
            self._event_progress = {sensor.id: 0 for sensor in self.sensors}
        self.event_started.emit(markers, event)

    def stop(self):
        self.requestInterruption()

    def _background_chunk(self, sensor: Sensor, axis: str, n: int) -> np.ndarray:
        rng = self._noise_rng[sensor.id][axis]
        white = rng.standard_normal(n)
        dt = 1.0 / sensor.sampling_rate
        alpha = math.exp(-2 * math.pi * self.background_freq * dt)
        b, a = [1 - alpha], [1, -alpha]
        zi = self._noise_zi[sensor.id][axis]
        filtered, zf = lfilter(b, a, white, zi=zi)
        self._noise_zi[sensor.id][axis] = zf
        return filtered * self.background_std

    def _event_chunk(self, sensor: Sensor, axis: str, n: int) -> Optional[np.ndarray]:
        if self._event_waveforms is None:
            return None
        wf = self._event_waveforms[sensor.id][axis]["acc"]
        start = self._event_progress[sensor.id]
        if start >= len(wf):
            return None
        end = min(start + n, len(wf))
        piece = wf[start:end]
        if len(piece) < n:
            piece = np.pad(piece, (0, n - len(piece)))
        return piece

    def _interruptible_sleep(self, seconds: float):
        ms_total = int(seconds * 1000)
        slept = 0
        while slept < ms_total and not self.isInterruptionRequested():
            chunk = min(50, ms_total - slept)
            QThread.msleep(chunk)
            slept += chunk

    def run(self):
        chunk_n = {s.id: max(1, int(round(self.update_interval * s.sampling_rate)))
                   for s in self.sensors}
        next_tick = time.monotonic()
        print(f"[SeismicDataGenerator] run() стартовал, sensors={[s.id for s in self.sensors]}")

        while not self.isInterruptionRequested():
            payload = {}
            with self._lock:
                event_active = self._event_waveforms is not None
                any_event_alive = False
                for sensor in self.sensors:
                    n = chunk_n[sensor.id]
                    start_sample = self._sample_count[sensor.id]
                    t_abs = (start_sample + np.arange(n)) / sensor.sampling_rate
                    axis_payload = {}
                    for axis in AXES:
                        acc = self._background_chunk(sensor, axis, n)
                        if event_active:
                            ev_piece = self._event_chunk(sensor, axis, n)
                            if ev_piece is not None:
                                acc = acc + ev_piece
                                any_event_alive = True
                        axis_payload[axis] = (t_abs, acc)
                    payload[sensor.id] = axis_payload
                    self._sample_count[sensor.id] += n
                    if event_active:
                        self._event_progress[sensor.id] += n
                if event_active and not any_event_alive:
                    self._event_waveforms = None
                    self._event_progress = {}

            self.new_chunk.emit(payload)

            next_tick += self.update_interval
            sleep_time = next_tick - time.monotonic()
            if sleep_time > 0:
                self._interruptible_sleep(sleep_time)
            else:
                next_tick = time.monotonic()

        print("[SeismicDataGenerator] run() завершён")
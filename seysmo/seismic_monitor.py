# seismic_monitor.py
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
from pathlib import Path
import math
import numpy as np
from scipy.signal import lfilter
import logging

# ====================== СИМУЛЯТОР (расширенный) ======================





@dataclass
class Sensor:
    id: str
    x: float  # км (восток)
    y: float  # км (север)
    z: float = 0.0
    sampling_rate: float = 100.0


@dataclass
class EarthquakeEvent:
    magnitude: float
    x: float
    y: float
    depth: float
    seed: int = 42


class SeismicSimulator:
    def __init__(self, vp: float = 6.0, vs: float = 3.5, q: float = 150):
        self.vp = vp
        self.vs = vs
        self.q = q
        logging.basicConfig(level=logging.INFO)

    def _calc_distance(self, event: EarthquakeEvent, sensor: Sensor) -> float:
        return math.hypot(event.x - sensor.x, event.y - sensor.y, event.depth - sensor.z)

    def _generate_waveform(self, event: EarthquakeEvent, sensor: Sensor, axis: str) -> Tuple[np.ndarray, np.ndarray, float]:
        rng = np.random.default_rng(event.seed + {"Z": 100, "N": 200, "E": 300}[axis])
        dist = max(self._calc_distance(event, sensor), 0.5)

        t_p = dist / self.vp
        t_s = dist / self.vs
        duration = t_s + 10.0  # чуть больше запаса
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
        alpha = math.exp(-2 * math.pi * freq * dt)
        b, a = [1 - alpha], [1, -alpha]
        noise = lfilter(b, a, noise)

        waveform = envelope * noise
        bg_noise = rng.normal(0, 1e-4 * pga_target, n_samples)
        waveform += bg_noise

        return waveform, t, pga_target, t_p, t_s

    def generate_event_data(self, event: EarthquakeEvent, sensors: List[Sensor]) -> Dict:
        """Генерирует данные для всех датчиков и возвращает в удобном виде"""
        data = {}
        for sensor in sensors:
            sensor_data = {}
            for axis in ("Z", "N", "E"):
                acc, t, pga, t_p, t_s = self._generate_waveform(event, sensor, axis)
                vel = np.cumsum(acc) / sensor.sampling_rate
                if len(acc) > 20:
                    vel -= np.convolve(vel, np.ones(20)/20, mode='same')

                sensor_data[axis] = {
                    "time": t,
                    "acc": acc,
                    "vel": vel,
                    "t_p": t_p,
                    "t_s": t_s,
                    "pga": float(np.max(np.abs(acc)))
                }
            data[sensor.id] = sensor_data
        return data


# ====================== ПРИЛОЖЕНИЕ ======================
import sys
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, 
                           QWidget, QPushButton, QLabel, QComboBox, QGroupBox)
from PyQt6.QtCore import QThread, pyqtSignal, QTimer
import pyqtgraph as pg
import time


class SeismicWorker(QThread):
    update_signal = pyqtSignal(str, str, np.ndarray, np.ndarray)  # sensor_id, axis, t, acc
    event_finished = pyqtSignal()

    def __init__(self, simulator: SeismicSimulator, sensors: List[Sensor]):
        super().__init__()
        self.simulator = simulator
        self.sensors = sensors
        self.current_event_data = None
        self.is_running = False
        self.playback_speed = 1.0  # 1.0 = реальное время

    def run_event(self, event: EarthquakeEvent):
        self.current_event_data = self.simulator.generate_event_data(event, self.sensors)
        self.is_running = True
        self.start()

    def run(self):
        if not self.current_event_data:
            return

        start_time = time.time()
        max_duration = 60.0  # 60 секунд истории

        while self.is_running:
            elapsed = (time.time() - start_time) * self.playback_speed

            for sensor_id, sensor_data in self.current_event_data.items():
                for axis in ["Z"]:  # сейчас только Z
                    ch = sensor_data[axis]
                    # Берём данные до текущего момента
                    mask = ch["time"] <= elapsed
                    if np.any(mask):
                        t_slice = ch["time"][mask]
                        acc_slice = ch["acc"][mask]
                        self.update_signal.emit(sensor_id, axis, t_slice, acc_slice)

            if elapsed > max_duration:
                break

            time.sleep(0.05)  # ~20 FPS обновлений

        self.event_finished.emit()


class SeismicMonitorApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Сейсмический Монитор — Реальное Время")
        self.resize(1400, 800)

        # Датчики (можно менять)
        self.sensors = [
            Sensor(id="STN_A", x=10.0, y=8.0, sampling_rate=100),
            Sensor(id="STN_B", x=15.0, y=12.0, sampling_rate=200),
            Sensor(id="STN_C", x=12.5, y=8.1, sampling_rate=100),
        ]

        self.simulator = SeismicSimulator(vp=6.0, vs=3.5, q=120)
        self.worker = SeismicWorker(self.simulator, self.sensors)
        self.worker.update_signal.connect(self.update_plot)
        self.worker.event_finished.connect(self.on_event_finished)

        self.plots = {}
        self.curves = {}
        self.setup_ui()

        self.current_event = None
        self.last_trigger_time = None

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Управление
        ctrl_layout = QHBoxLayout()
        self.btn_trigger = QPushButton("🔴 Запустить Землетрясение (тест)")
        self.btn_trigger.clicked.connect(self.test_earthquake)
        ctrl_layout.addWidget(self.btn_trigger)

        self.axis_selector = QComboBox()
        self.axis_selector.addItems(["Z (Вертикаль)", "N (Север)", "E (Восток)"])
        self.axis_selector.currentTextChanged.connect(self.change_axis)
        ctrl_layout.addWidget(QLabel("Канал:"))
        ctrl_layout.addWidget(self.axis_selector)

        layout.addLayout(ctrl_layout)

        # Графики
        plots_layout = QHBoxLayout()
        for sensor in self.sensors:
            group = QGroupBox(sensor.id)
            vbox = QVBoxLayout(group)
            plot = pg.PlotWidget()
            plot.setBackground('k')
            plot.setLabel('left', 'Ускорение', 'м/с²')
            plot.setLabel('bottom', 'Время', 'с')
            plot.showGrid(x=True, y=True, alpha=0.5)
            plot.addLegend()

            curve = plot.plot(pen=pg.mkPen(color='#00ff00', width=1.5), name='Acceleration')
            vbox.addWidget(plot)
            plots_layout.addWidget(group)

            self.plots[sensor.id] = plot
            self.curves[sensor.id] = curve

        layout.addLayout(plots_layout)

        # Инфо панель
        self.info_label = QLabel("Готов к работе. Ожидание события...")
        self.info_label.setStyleSheet("font-size: 14px; padding: 8px;")
        layout.addWidget(self.info_label)

    def change_axis(self, text):
        # Пока только Z, но задел на будущее
        pass

    def test_earthquake(self):
        """Тестовое событие"""
        event = EarthquakeEvent(
            magnitude=5.8,
            x=12.8,
            y=9.2,
            depth=12.0,
            seed=int(time.time())
        )
        self.trigger_earthquake(event.x, event.y, event.depth, event.magnitude, event.seed)

    def trigger_earthquake(self, x: float, y: float, depth: float, magnitude: float, seed: Optional[int] = None):
        """Главный публичный метод"""
        if seed is None:
            seed = int(time.time())

        event = EarthquakeEvent(magnitude=magnitude, x=x, y=y, depth=depth, seed=seed)
        self.current_event = event
        self.last_trigger_time = time.time()

        logging.info(f"Землетрясение! M{magnitude:.1f} в ({x:.1f}, {y:.1f}), глубина {depth} км")

        self.info_label.setText(
            f"🔴 Землетрясение M{magnitude:.1f} | Эпицентр: ({x:.1f}, {y:.1f}) | Глубина: {depth} км"
        )

        # Сброс графиков
        for curve in self.curves.values():
            curve.setData([], [])

        self.worker.run_event(event)

    def update_plot(self, sensor_id: str, axis: str, t: np.ndarray, acc: np.ndarray):
        if sensor_id in self.curves:
            self.curves[sensor_id].setData(t, acc)

    def on_event_finished(self):
        self.info_label.setText("✅ Событие завершено. Готов к следующему.")
        self.worker.is_running = False


# ====================== ЗАПУСК ======================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    # Тёмная тема
    app.setStyle("Fusion")
    window = SeismicMonitorApp()
    window.show()
    sys.exit(app.exec())
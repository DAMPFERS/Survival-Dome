"""
SeismicMonitorWidget — переиспользуемый виджет.
Оптимизирован: отрисовка идёт по QTimer (10 Гц), а не по каждому чанку.
"""
from typing import List, Dict, Optional
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt, QTimer
from seismic_core import Sensor, AXES
from seismic_generator import SeismicDataGenerator

AXIS_LABELS = {"Z": "Z (Вертикаль)", "N": "N (Север-Юг)", "E": "E (Восток-Запад)"}
AXIS_COLORS = {"Z": "#00ff66", "N": "#00c8ff", "E": "#ffaa00"}


class SensorBuffer:
    """Кольцевой буфер для одного датчика — хранит ВСЕ 3 оси всегда."""
    def __init__(self, history_window: float):
        self.history_window = history_window
        self.t: Dict[str, np.ndarray] = {ax: np.array([], dtype=np.float64) for ax in AXES}
        self.acc: Dict[str, np.ndarray] = {ax: np.array([], dtype=np.float64) for ax in AXES}

    def append(self, axis: str, t_chunk: np.ndarray, acc_chunk: np.ndarray):
        self.t[axis] = np.concatenate([self.t[axis], t_chunk])
        self.acc[axis] = np.concatenate([self.acc[axis], acc_chunk])
        if self.t[axis].size:
            cutoff = self.t[axis][-1] - self.history_window
            idx = np.searchsorted(self.t[axis], cutoff, side="left")
            if idx > 0:
                self.t[axis] = self.t[axis][idx:]
                self.acc[axis] = self.acc[axis][idx:]

    def latest_time(self) -> float:
        for ax in AXES:
            if self.t[ax].size:
                return float(self.t[ax][-1])
        return 0.0


class SeismicMonitorWidget(QWidget):
    def __init__(self,
                 sensors: List[Sensor],
                 history_window: float = 60.0,
                 vp: float = 6.0,
                 vs: float = 3.5,
                 parent=None):
        super().__init__(parent)
        self.sensors = sensors
        self.history_window = history_window

        self.buffers: Dict[str, SensorBuffer] = {s.id: SensorBuffer(history_window) for s in sensors}
        self.current_axis = "Z"
        self._markers: Dict[str, list] = {s.id: [] for s in sensors}
        self.plots: Dict[str, pg.PlotWidget] = {}
        self.curves: Dict[str, pg.PlotDataItem] = {}

        self._build_ui()

        # Генератор данных
        self.generator = SeismicDataGenerator(sensors=sensors, vp=vp, vs=vs)
        self.generator.new_chunk.connect(self._on_new_chunk)
        self.generator.event_started.connect(self._on_event_started)
        self.generator.start()

        # Таймер отрисовки — 10 Гц (в 2 раза реже, чем поток данных)
        self._redraw_timer = QTimer(self)
        self._redraw_timer.timeout.connect(self._redraw_all)
        self._redraw_timer.start(100)

        # Таймер чистки маркеров — 1 Гц
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.timeout.connect(self._cleanup_old_markers)
        self._cleanup_timer.start(1000)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        first_plot = None
        for sensor in self.sensors:
            plot = pg.PlotWidget()
            plot.setBackground("k")
            plot.setLabel("left", "Ускорение", units="m/s²")
            plot.setLabel("bottom", "Время", units="s")
            plot.showGrid(x=True, y=True, alpha=0.4)
            plot.setTitle(f"{sensor.id}  (x={sensor.x:.1f} км, y={sensor.y:.1f} км, "
                          f"{sensor.sampling_rate:.0f} Гц)", color="#cccccc", size="10pt")
            plot.setMouseEnabled(x=True, y=True)

            # КЛЮЧЕВЫЕ ОПТИМИЗАЦИИ PyQtGraph:
            # plot.setDownsampling(auto=True, method="peak")  # прореживание при зуме
            # plot.setClipToView(True)                         # рисуем только видимое
            # plot.setRange(yRange=(-0.01, 0.01))              # фикс. диапазон по Y
            # Оптимизации PyQtGraph (совместимо со всеми версиями):
            # Оптимизации PyQtGraph (совместимо со всеми версиями):
            plot.setDownsampling(auto=True)     # автопрореживание точек
            plot.setClipToView(True)            # рисуем только видимую область
            plot.enableAutoRange(y=False)       # отключаем авто-масштаб по Y
            plot.setYRange(-0.01, 0.01)         # фиксированный диапазон по Y (м/с²)        # фиксированный диапазон по Y (м/с²)
            
            curve = plot.plot(pen=pg.mkPen(color=AXIS_COLORS["Z"], width=1.2), name="acc")
            if first_plot is None:
                first_plot = plot
            else:
                plot.setXLink(first_plot)
            layout.addWidget(plot, stretch=1)
            self.plots[sensor.id] = plot
            self.curves[sensor.id] = curve

        self.status_label = QLabel("Готов к работе. Фоновая активность — норма.")
        self.status_label.setStyleSheet("font-size: 13px; padding: 6px; color: #cccccc;")
        layout.addWidget(self.status_label)

    # ------------------------------------------------------------------ #
    # Публичное API
    # ------------------------------------------------------------------ #
    def trigger_earthquake(self, x: float, y: float, depth: float,
                           magnitude: float, seed: Optional[int] = None):
        self.generator.trigger_earthquake(x=x, y=y, depth=depth,
                                          magnitude=magnitude, seed=seed)

    def set_axis(self, axis: str):
        if axis not in AXES:
            return
        self.current_axis = axis
        for sensor in self.sensors:
            self.curves[sensor.id].setPen(pg.mkPen(color=AXIS_COLORS[axis], width=1.2))
            self.plots[sensor.id].setLabel("left", f"Ускорение ({AXIS_LABELS[axis]})", units="m/s²")
        self._redraw_all()

    def shutdown(self):
        self._redraw_timer.stop()
        self._cleanup_timer.stop()
        self.generator.stop()
        if not self.generator.wait(2000):
            print("[SeismicMonitorWidget] WARNING: генератор не завершился, принудительно...")
            self.generator.terminate()
            self.generator.wait(500)

    # ------------------------------------------------------------------ #
    # Слоты
    # ------------------------------------------------------------------ #
    def _on_new_chunk(self, payload: dict):
        """Просто складываем данные в буфер. Отрисовка — по таймеру."""
        for sensor_id, axes_data in payload.items():
            buf = self.buffers[sensor_id]
            for axis, (t_chunk, acc_chunk) in axes_data.items():
                buf.append(axis, t_chunk, acc_chunk)

    def _on_event_started(self, markers: dict, event):
        for sensor_id, m in markers.items():
            plot = self.plots[sensor_id]
            p_line = pg.InfiniteLine(pos=m["t_p_abs"], angle=90,
                                     pen=pg.mkPen("#ffffff", width=1, style=Qt.PenStyle.DashLine),
                                     label="P", labelOpts={"color": "#ffffff", "position": 0.95})
            s_line = pg.InfiniteLine(pos=m["t_s_abs"], angle=90,
                                     pen=pg.mkPen("#ff4444", width=1, style=Qt.PenStyle.DashLine),
                                     label="S", labelOpts={"color": "#ff4444", "position": 0.95})
            plot.addItem(p_line)
            plot.addItem(s_line)
            self._markers[sensor_id].append((p_line, m["t_p_abs"]))
            self._markers[sensor_id].append((s_line, m["t_s_abs"]))

        sp_delay = markers[self.sensors[0].id]["t_s_abs"] - markers[self.sensors[0].id]["t_p_abs"]
        self.status_label.setText(
            f"🔴 Землетрясение M{event.magnitude:.1f} | Эпицентр: ({event.x:.1f}, {event.y:.1f}) км | "
            f"Глубина: {event.depth:.1f} км | S-P на {self.sensors[0].id}: {sp_delay:.2f} с"
        )

    def _redraw_all(self):
        now = 0.0
        for sensor in self.sensors:
            buf = self.buffers[sensor.id]
            now = max(now, buf.latest_time())
            t = buf.t[self.current_axis]
            acc = buf.acc[self.current_axis]
            self.curves[sensor.id].setData(t, acc)

        x_min = max(0.0, now - self.history_window)
        for plot in self.plots.values():
            plot.setXRange(x_min, max(now, x_min + 1.0), padding=0)

    def _cleanup_old_markers(self):
        for sensor in self.sensors:
            plot = self.plots[sensor.id]
            now = self.buffers[sensor.id].latest_time()
            cutoff = now - self.history_window
            keep = []
            for line, t_abs in self._markers[sensor.id]:
                if t_abs < cutoff:
                    plot.removeItem(line)
                else:
                    keep.append((line, t_abs))
            self._markers[sensor.id] = keep
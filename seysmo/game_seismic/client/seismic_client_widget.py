"""
seismic_client_widget.py — главный виджет клиента-аналитика.

Принимает данные по TCP, анализирует (STA/LTA, пикинг, локация), отображает графики, STA/LTA кривую, карту, информацию.
"""
from typing import List, Dict, Optional
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSplitter
from PyQt6.QtCore import Qt, QTimer
from ..common.seismic_core import Sensor, AXES
from .tcp_client import TCPClientThread
from .detector import EventDetector, EventDetection
from .locator import HypocenterLocator, HypocenterSolution

AXIS_LABELS = {"Z": "Z (Вертикаль)", "N": "N (Север-юг)", "E": "E (Восток-запад)"}
AXIS_COLORS = {"Z": "#00ff66", "N": "#00c8ff", "E": "#ffaa00"}

class SensorBuffer:
    """
    Кольцевой буфер для хранения данных по одной оси — хранит последние 3 оси сразу.
    """

    def __init__(self, history_window: float):
        self.history_window = history_window
        self.t: Dict[str, np.ndarray] = {ax: np.array([], dtype=np.float64) for ax in AXES}
        self.acc: Dict[str, np.ndarray] = {ax: np.array([], dtype=np.float64) for ax in AXES}

    def append(self, axis: str, t_chunk: np.ndarray, acc_chunk: np.ndarray):
        self.t[axis] = np.concatenate([self.t[axis], t_chunk])
        self.acc[axis] = np.concatenate([self.acc[axis], acc_chunk])

        # Ограничиваем по времени
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

class SeismicClientWidget(QWidget):
    """
    Главный виджет клиента.

    Принимает данные из TCPClientThread, анализирует через EventDetector,
    определяет гипоцентр через HypocenterLocator, отображает всё в GUI.
    """

    def __init__(self,
                 sensors: List[Sensor],
                 tcp_host: str = '127.0.0.1',
                 tcp_port: int = 9001,
                 history_window: float = 60.0,
                 parent=None):
        super().__init__(parent)
        self.sensors = sensors
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.history_window = history_window

        # Буферы данных
        self.buffers: Dict[str, SensorBuffer] = {s.id: SensorBuffer(history_window) for s in sensors}

        # Текущая ось для отображения
        self.current_axis = "Z"

        # Маркеры P/S на графиках
        self._markers: Dict[str, list] = {s.id: [] for s in sensors}

        # Детектор событий
        self.detector = EventDetector(sampling_rate=sensors[0].sampling_rate)

        # Локатор гипоцентра
        self.locator = HypocenterLocator(vp=6.0)

        # Позиции датчиков (для локатора)
        self.sensor_positions: Dict[str, tuple] = {
            s.id: (s.x, s.y, s.z) for s in sensors
        }

        # Последнее обнаруженное событие
        self._last_hypocenter: Optional[HypocenterSolution] = None
        self._p_arrivals: Dict[str, float] = {}
        self._pga_by_sensor: Dict[str, float] = {}

        # GUI
        self.plots: Dict[str, pg.PlotWidget] = {}
        self.curves: Dict[str, pg.PlotDataItem] = {}
        self.sta_lta_plot: Optional[pg.PlotWidget] = None
        self.sta_lta_curve: Optional[pg.PlotDataItem] = None
        self.map_plot: Optional[pg.PlotWidget] = None
        self.info_label: Optional[QLabel] = None
        self.status_label: Optional[QLabel] = None

        self._build_ui()

        # TCP-клиент
        self.tcp_client = TCPClientThread(host=tcp_host, port=tcp_port)
        self.tcp_client.chunk_received.connect(self._on_chunk_received)
        self.tcp_client.connection_status.connect(self._on_connection_status)
        self.tcp_client.start()

        # Таймер перерисовки — 10 Гц (в 100 мс)
        self._redraw_timer = QTimer(self)
        self._redraw_timer.timeout.connect(self._redraw_all)
        self._redraw_timer.start(100)

        # Таймер чистки старых маркеров — 1 Гц
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.timeout.connect(self._cleanup_old_markers)
        self._cleanup_timer.start(1000)

    def _build_ui(self):
        """
        Строит GUI: графики, STA/LTA, карта, информация.
        """
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)

        # Разделитель: слева графики, справа карта + информация
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # === ЛЕВАЯ ЧАСТЬ: графики ===
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        first_plot = None
        for sensor in self.sensors:
            plot = pg.PlotWidget()
            plot.setBackground("k")
            plot.setLabel("left", "Ускорение", units="m/s²")
            plot.setLabel("bottom", "Время", units="s")
            plot.showGrid(x=True, y=True, alpha=0.4)
            plot.setTitle(f"{sensor.id}  (x={sensor.x:.1f}, y={sensor.y:.1f}) км",
                         color="#cccccc", size="10pt")
            plot.setMouseEnabled(x=True, y=True)

            # Оптимизации PyQtGraph
            plot.setDownsampling(auto=True)
            plot.setClipToView(True)
            plot.enableAutoRange(y=False)
            plot.setYRange(-0.01, 0.01)

            curve = plot.plot(pen=pg.mkPen(color=AXIS_COLORS["Z"], width=1.2), name="acc")

            if first_plot is None:
                first_plot = plot
            else:
                plot.setXLink(first_plot)

            left_layout.addWidget(plot, stretch=1)
            self.plots[sensor.id] = plot
            self.curves[sensor.id] = curve

        # STA/LTA график (для первого датчика)
        self.sta_lta_plot = pg.PlotWidget()
        self.sta_lta_plot.setBackground("k")
        self.sta_lta_plot.setLabel("left", "STA/LTA")
        self.sta_lta_plot.setLabel("bottom", "Время", units="s")
        self.sta_lta_plot.showGrid(x=True, y=True, alpha=0.4)
        self.sta_lta_plot.setTitle("STA/LTA детектор (STA_01, ось Z)",
                                   color="#cccccc", size="10pt")
        self.sta_lta_plot.setMouseEnabled(x=True, y=True)
        self.sta_lta_plot.setDownsampling(auto=True)
        self.sta_lta_plot.setClipToView(True)
        self.sta_lta_plot.setXLink(first_plot)

        self.sta_lta_curve = self.sta_lta_plot.plot(
            pen=pg.mkPen(color="#ff00ff", width=1.5), name="STA/LTA"
        )

        # Горизонтальная линия порога
        self.sta_lta_plot.addItem(pg.InfiniteLine(
            pos=self.detector.trigger_threshold, angle=0,
            pen=pg.mkPen("#ff4444", width=1, style=Qt.PenStyle.DashLine),
            label=f"Порог: {self.detector.trigger_threshold}",
            labelOpts={"color": "#ff4444", "position": 0.95}
        ))

        left_layout.addWidget(self.sta_lta_plot, stretch=1)

        splitter.addWidget(left_widget)

        # === ПРАВАЯ ЧАСТЬ: карта + информация ===
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Карта
        self.map_plot = pg.PlotWidget()
        self.map_plot.setBackground("k")
        self.map_plot.setLabel("left", "Y (Север)", units="км")
        self.map_plot.setLabel("bottom", "X (Восток)", units="км")
        self.map_plot.showGrid(x=True, y=True, alpha=0.4)
        self.map_plot.setTitle("Карта датчиков и эпицентра",
                              color="#cccccc", size="10pt")
        self.map_plot.setAspectLocked(True)
        self.map_plot.setXRange(0, 50)
        self.map_plot.setYRange(0, 50)

        # Рисуем датчики
        for sensor in self.sensors:
            self.map_plot.plot(
                [sensor.x], [sensor.y],
                pen=None, symbol='o', symbolSize=12, symbolBrush='#00ff66'
            )
            # Подписи: сдвигаем, чтобы не загораживать точки
            text_item = pg.TextItem(sensor.id, color='#00ff66', anchor=(0, 1))
            text_item.setPos(sensor.x + 0.5, sensor.y - 0.5)
            self.map_plot.addItem(text_item)

        # Маркер эпицентра (будет обновляться)
        self._epicenter_marker = self.map_plot.plot(
            [], [], pen=None, symbol='+', symbolSize=20, symbolBrush='#ff4444'
        )
        self._epicenter_label = pg.TextItem("", color='#ff4444', anchor=(0, 1))
        self.map_plot.addItem(self._epicenter_label)

        right_layout.addWidget(self.map_plot, stretch=2)

        # Информация о событии
        self.info_label = QLabel("Ожидание данных...")
        self.info_label.setStyleSheet(
            "font-size: 12px; padding: 10px; background: #2a2a2a; border-radius: 4px;"
        )
        self.info_label.setWordWrap(True)
        right_layout.addWidget(self.info_label, stretch=1)

        splitter.addWidget(right_widget)
        splitter.setSizes([900, 400])

        main_layout.addWidget(splitter, stretch=1)

        # === НИЖНЯЯ ПАНЕЛЬ: статус ===
        self.status_label = QLabel("Подключение к серверу...")
        self.status_label.setStyleSheet("font-size: 13px; padding: 6px; color: #cccccc;")
        main_layout.addWidget(self.status_label)

    def set_axis(self, axis: str):
        """
        Переключает отображаемую ось.
        """
        if axis not in AXES:
            return
        self.current_axis = axis
        for sensor in self.sensors:
            self.curves[sensor.id].setPen(pg.mkPen(color=AXIS_COLORS[axis], width=1.2))
            self.plots[sensor.id].setLabel("left", f"Ускорение ({AXIS_LABELS[axis]})", units="m/s²")
        self._redraw_all()

    def shutdown(self):
        """
        Корректное завершение работы виджетов.
        """
        self._redraw_timer.stop()
        self._cleanup_timer.stop()

        self.tcp_client.requestInterruption()
        if not self.tcp_client.wait(2000):
            print("[SeismicClientWidget] WARNING: TCP-клиент не завершился, принудительно...")
            self.tcp_client.terminate()
            self.tcp_client.wait(500)

    # ------------------------------------------------------------------ #
    # Слоты
    # ------------------------------------------------------------------ #

    def _on_chunk_received(self, chunk: dict):
        sensor_id = chunk['sensor_id']
        timestamp = chunk['timestamp']
        z = chunk['Z']
        n = chunk['N']
        e = chunk['E']

        # Находим сенсор по ID
        sensor = next(s for s in self.sensors if s.id == sensor_id)
        sampling_rate = sensor.sampling_rate

        # Добавляем в буфер
        buf = self.buffers[sensor_id]
        t_z = np.arange(len(z)) / sampling_rate + timestamp
        t_n = np.arange(len(n)) / sampling_rate + timestamp
        t_e = np.arange(len(e)) / sampling_rate + timestamp
        buf.append('Z', t_z, z)
        buf.append('N', t_n, n)
        buf.append('E', t_e, e)

        # Обрабатываем детектором
        detection = self.detector.process_chunk(sensor_id, timestamp, z, n, e, chunk_length=len(z))

        if detection is not None:
            self._on_event_detected(detection)

    def _on_event_detected(self, detection: EventDetection):
        """
        Слот: детектор обнаружил событие на одном датчике.
        """
        sensor_id = detection.sensor_id

        print(f"\n[Client] Event detected on {sensor_id}:")
        print(f"  Trigger time: {detection.trigger_time:.2f}s")
        print(f"  P-wave time: {detection.p_wave_time}")
        print(f"  S-wave time: {detection.s_wave_time}")
        print(f"  PGA: {detection.pga:.6f} m/s²")

        # Добавляем маркеры P и S на график
        plot = self.plots[sensor_id]

        if detection.p_wave_time is not None:
            p_line = pg.InfiniteLine(
                pos=detection.p_wave_time, angle=90,
                pen=pg.mkPen("#ffffff", width=1.5, style=Qt.PenStyle.DashLine),
                label="P (detected)", labelOpts={"color": "#ffffff", "position": 0.95}
            )
            plot.addItem(p_line)
            self._markers[sensor_id].append((p_line, detection.p_wave_time))
            self._p_arrivals[sensor_id] = detection.p_wave_time
            print(f"  Added P marker at {detection.p_wave_time:.2f}s")

        if detection.s_wave_time is not None:
            s_line = pg.InfiniteLine(
                pos=detection.s_wave_time, angle=90,
                pen=pg.mkPen("#ff4444", width=1.5, style=Qt.PenStyle.DashLine),
                label="S (detected)", labelOpts={"color": "#ff4444", "position": 0.95}
            )
            plot.addItem(s_line)
            self._markers[sensor_id].append((s_line, detection.s_wave_time))
            print(f"  Added S marker at {detection.s_wave_time:.2f}s")

        # Сохраняем PGA для датчика
        self._pga_by_sensor[sensor_id] = detection.pga

        # Пытаемся определить гипоцентр, если есть данные с 3+ датчиков
        if len(self._p_arrivals) >= 3:
            print(f"\n[Client] Attempting hypocenter location with {len(self._p_arrivals)} sensors:")
            for sid, t_p in self._p_arrivals.items():
                print(f"  {sid}: P-arrival at {t_p:.2f}s")

            hypocenter = self.locator.locate(self._p_arrivals, self.sensor_positions)

            if hypocenter is not None:
                # Оцениваем магнитуду
                hypocenter.magnitude = self.locator.estimate_magnitude(
                    hypocenter, self._pga_by_sensor, self.sensor_positions
                )

                self._last_hypocenter = hypocenter
                self._update_map(hypocenter)
                self._update_info(hypocenter)

                print(f"\n[Client] Hypocenter located:")
                print(f"  Position: ({hypocenter.x:.2f}, {hypocenter.y:.2f}) km")
                print(f"  Depth: {hypocenter.depth:.2f} km")
                print(f"  Origin time: {hypocenter.origin_time:.2f} s")
                print(f"  Magnitude: M{hypocenter.magnitude:.2f}")
                print(f"  Residual: {hypocenter.residual:.4f} s")
            else:
                print(f"[Client] Failed to locate hypocenter")

    def _on_connection_status(self, status: str):
        """
        Слот: статус подключения TCP.
        """
        if status == 'connected':
            self.status_label.setText(f"✅ Подключено к {self.tcp_host}:{self.tcp_port}")
            self.status_label.setStyleSheet("font-size: 13px; padding: 6px; color: #00ff66;")
        elif status == 'disconnected':
            self.status_label.setText("⚠️ Отключено от сервера. Повторная попытка...")
            self.status_label.setStyleSheet("font-size: 13px; padding: 6px; color: #ffaa00;")
        else:
            self.status_label.setText(f"❌ Ошибка: {status}")
            self.status_label.setStyleSheet("font-size: 13px; padding: 6px; color: #ff4444;")

    def _redraw_all(self):
        """
        Перерисовка всех графиков (10 Гц).
        """
        now = 0.0
        for sensor in self.sensors:
            buf = self.buffers[sensor.id]
            now = max(now, buf.latest_time())

            t = buf.t[self.current_axis]
            acc = buf.acc[self.current_axis]
            self.curves[sensor.id].setData(t, acc)

        # Устанавливаем диапазон по X
        x_min = max(0.0, now - self.history_window)
        for plot in self.plots.values():
            plot.setXRange(x_min, max(now, x_min + 1.0), padding=0)

        # Обновляем STA/LTA кривую
        if self.sta_lta_plot and self.sta_lta_curve:
            sta_lta = self.detector.get_sta_lta_curve(self.sensors[0].id)
            if sta_lta is not None:
                buf = self.buffers[self.sensors[0].id]
                t = buf.t['Z']
                if len(t) == len(sta_lta):
                    self.sta_lta_curve.setData(t, sta_lta)
                    self.sta_lta_plot.setXRange(x_min, max(now, x_min + 1.0), padding=0)

    def _cleanup_old_markers(self):
        """
        Удаляет старые маркеры P/S (1 Гц).
        """
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

    def _update_map(self, hypocenter: HypocenterSolution):
        """
        Обновляет маркер эпицентра на карте.
        """
        self._epicenter_marker.setData([hypocenter.x], [hypocenter.y])
        self._epicenter_label.setPos(hypocenter.x + 1.0, hypocenter.y - 1.0)
        self._epicenter_label.setText(f"M{hypocenter.magnitude:.1f}")

    def _update_info(self, hypocenter: HypocenterSolution):
        """
        Обновляет информационную панель.
        """
        text = f"""<b>Обнаруженное событие:</b><br>
        Эпицентр: ({hypocenter.x:.1f}, {hypocenter.y:.1f}) км<br>
        Глубина: {hypocenter.depth:.1f} км<br>
        Магнитуда: M{hypocenter.magnitude:.1f}<br>
        Время возникновения: {hypocenter.origin_time:.2f} с<br>
        Невязка (RMS): {hypocenter.residual:.3f} с<br>
        <br>
        <b>Время прихода P-волн:</b><br>
        """
        for sensor_id, t_p in self._p_arrivals.items():
            text += f"{sensor_id}: {t_p:.2f} с<br>"

        self.info_label.setText(text)
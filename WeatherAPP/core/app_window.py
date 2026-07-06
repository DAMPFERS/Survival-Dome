import time

from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QPainter, QColor
from PyQt6.QtCore import QTimer

from core.config import *
from core.layers import LayerManager
from rendering.map_renderer import MapRenderer
from simulation.weather_engine import WeatherEngine
from rendering.wind_renderer import WindRenderer
from rendering.dust_renderer import DustRenderer


class AppWindow(QWidget):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Weather Visualizer")

        self.setMinimumSize(
            WINDOW_WIDTH,
            WINDOW_HEIGHT
        )

        self.layer_manager = LayerManager()

        self.weather_engine = WeatherEngine()

        self.map_renderer = MapRenderer(MAP_PATH)

        self.last_frame_time = time.time()

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(int(1000 / FPS))
        
        self.wind_renderer = WindRenderer()
        
        self.dust_renderer = DustRenderer()

    def update_frame(self):

        current_time = time.time()

        delta_time = current_time - self.last_frame_time

        self.last_frame_time = current_time

        self.weather_engine.update(delta_time)

        self.update()

    def paintEvent(self, event):

        painter = QPainter(self)

        painter.setRenderHint(
            QPainter.RenderHint.Antialiasing
        )

        painter.fillRect(
            self.rect(),
            QColor(BACKGROUND_COLOR)
        )

        self.map_renderer.render(
            painter,
            self.layer_manager
        )
        
        self.wind_renderer.render(
            painter,
            self.layer_manager,
            self.weather_engine.wind_field
        )
        
        self.dust_renderer.render(
            painter,
            self.layer_manager,
            self.weather_engine.dust_storms
        )

        painter.end()
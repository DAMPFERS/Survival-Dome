from PyQt6.QtGui import QPixmap, QPainter
from PyQt6.QtCore import QRectF

from core.layers import LayerType


class MapRenderer:
    def __init__(self, map_path: str):
        self.map_pixmap = QPixmap(map_path)

    def render(self, painter: QPainter, layer_manager):
        if layer_manager.is_enabled(LayerType.MAP):

            target = QRectF(
                0,
                0,
                painter.device().width(),
                painter.device().height()
            )

            painter.drawPixmap(
                target.toRect(),
                self.map_pixmap
            )
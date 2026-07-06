from PyQt6.QtGui import QColor, QPen
from PyQt6.QtCore import Qt

from core.layers import LayerType


class WindRenderer:

    def __init__(self):
        pass

    def render(
        self,
        painter,
        layer_manager,
        wind_field
    ):

        if not layer_manager.is_enabled(LayerType.WIND):
            return

        painter.setPen(
            QPen(
                QColor(120, 220, 255, 220),
                3.2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap
            )
        )

        for particle in wind_field.particles:

            x1 = particle.x
            y1 = particle.y

            x2 = x1 - particle.velocity_x * 0.15
            y2 = y1 - particle.velocity_y * 0.15

            painter.drawLine(
                int(x1),
                int(y1),
                int(x2),
                int(y2)
            )
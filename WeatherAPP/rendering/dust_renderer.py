from PyQt6.QtGui import QColor
from PyQt6.QtCore import Qt

from core.layers import LayerType


class DustRenderer:

    def __init__(self):
        pass

    def render(
        self,
        painter,
        layer_manager,
        dust_storms
    ):

        if not layer_manager.is_enabled(LayerType.DUST):
            return

        painter.setPen(Qt.PenStyle.NoPen)

        for storm in dust_storms:

            for particle in storm.particles:

                color = QColor(
                    252,
                    98,
                    84,
                    200
                    # int(particle.alpha)
                )

                painter.setBrush(color)

                painter.drawEllipse(
                    int(particle.x),
                    int(particle.y),
                    int(particle.size),
                    int(particle.size)
                )
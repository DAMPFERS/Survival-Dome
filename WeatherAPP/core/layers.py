from enum import Enum, auto


class LayerType(Enum):
    MAP = auto()
    WIND = auto()
    DUST = auto()
    ACID_RAIN = auto()
    UI = auto()


class LayerManager:
    def __init__(self):
        self.layers = {
            LayerType.MAP: True,
            LayerType.WIND: True,
            LayerType.DUST: True,
            LayerType.ACID_RAIN: True,
            LayerType.UI: True,
        }

    def is_enabled(self, layer: LayerType) -> bool:
        return self.layers.get(layer, False)

    def toggle(self, layer: LayerType):
        self.layers[layer] = not self.layers[layer]

    def set_enabled(self, layer: LayerType, enabled: bool):
        self.layers[layer] = enabled
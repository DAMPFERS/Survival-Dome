# dome_simulator/telemetry.py
"""
Слой телеметрии — то, что ВИДИТ агент (dome_crises.md, п. 3.2, доработки Д4, Д13, Д14).

StateStore хранит истинное состояние узлов (его читают узлы, правила и
админка). Агент получает не истину, а опубликованную телеметрию:

    истина → [FAULT-датчик: измерения = null] → [подмены показаний] → [заморозки] → телеметрия

Подмена показаний (кризисы типа A) — Distortion: константа, смещение, множитель,
дрейф, залипание, шум, null. Работает одинаково для виртуальных и реальных
узлов: реальные данные тоже попадают в StateStore, а искажаются при публикации.

Заморозки (кризисы типа D):
    - узел заморожен кризисом (№21, №30): status = UNKNOWN, updated_at не меняется;
    - основной канал купола (network_link_01) лёг: замирает вся телеметрия,
      команды агента не проходят (№23);
    - патчкорд щита повреждён: щит недоступен (№30);
    - сборщик телеметрии edge упал или edge перезагружается: замирают узлы,
      которые он обслуживает (К8, К3).

У каждого узла в телеметрии есть updated_at (часы симуляции) и status
(OK | UNKNOWN). Узлы, обслуживаемые edge, получают метки со сдвигом его
часов clock_offset_s (№20).

Интерлоки автоматики читают live_view(): подмены применены, заморозки — нет
(ПЛК опрашивает устройства напрямую, не через edge и внешний канал).
"""
from __future__ import annotations

import copy
import itertools
import math
import random
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .environment import Environment
from .nodes.base import NodeRegistry
from .nodes.sandbox import SensorNode
from .store import StateStore

# Узлы, чью телеметрию собирает telemetry_collector на edge_compute_01.
# Реальные устройства (щит, инвертор, АКБ, принтер, ЧПУ, основной датчик климата,
# вытяжка) опрашиваются своими драйверами напрямую.
EDGE_SERVED_NODES = frozenset({
    "solar_panels_01", "wind_turbine_01", "dizel_1", "fuel_tank_01",
    "climate_sensor_02", "climate_sensor_03", "air_quality_sensor_01",
    "smoke_detector_01", "smoke_detector_02", "weather_station_01", "seysmo_01",
    "water_pump_01", "water_tank_01", "water_filter_01", "fire_suppression_01",
    "access_control_01", "radiation_sensor_01", "chem_sensor_01",
    "material_inventory_01", "equipment_cooling_01", "supply_ventilation_01",
    "dome_sealing_01", "thermal_insulation_01", "radio_01", "backup_comms_01",
})
NETWORK_NODE = "network_link_01"
EDGE_NODE = "edge_compute_01"
PANEL_NODE = "smart_panel_01"

# Поля датчика, которые не обнуляются при FAULT (метаданные, а не измерения)
SENSOR_META_FIELDS = frozenset({
    "zone", "sensor_state", "confidence", "online", "data_available", "warning_threshold",
    "alarm_threshold", "threshold_mm_s", "reading_delay_s", "capacity_l", "spool_capacity_g",
    "spare_spools", "drift_temp_c", "drift_humidity_pct", "drift_co2_ppm", "alarm_state",
    "low_stock_warnings", "state", "substance", "event_type", "warming_up",
})
HIDDEN_PREFIX = "control_"

_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def parse_path(path: str) -> list[Any]:
    """'lines[1].voltage_v' -> ['lines', 1, 'voltage_v']; 'doors.STORAGE.state' -> [...]."""
    tokens: list[Any] = []
    for name, index in _PATH_TOKEN.findall(path):
        tokens.append(int(index) if index else name)
    if not tokens:
        raise ValueError(f"Пустой путь параметра: {path!r}")
    return tokens


def get_path(data: Any, path: str, default: Any = None) -> Any:
    cur = data
    for token in parse_path(path):
        try:
            cur = cur[token]
        except (KeyError, IndexError, TypeError):
            return default
    return cur


def set_path(data: Any, path: str, value: Any) -> bool:
    tokens = parse_path(path)
    cur = data
    for token in tokens[:-1]:
        try:
            cur = cur[token]
        except (KeyError, IndexError, TypeError):
            return False
    try:
        cur[tokens[-1]] = value
    except (KeyError, IndexError, TypeError):
        return False
    return True


@dataclass
class Distortion:
    """
    Подмена одного параметра телеметрии.

    mode:
        const       — value (+ шум noise)
        offset      — истина + value
        mult        — истина × value
        ramp_mult   — истина × k(t), k линейно от start до end за duration_s
        ramp_to     — истина + (target − истина) × min(1, t/duration_s)
        stuck       — значение, зафиксированное при первом применении (или value)
        noise       — истина + N(0, sigma)
        null        — None
        random_null — None с вероятностью p (при каждой публикации)
        func        — fn(истина, t_s, node_truth)
    expires_s — подмена снимается сама через столько секунд (провал на один отсчёт).
    """
    id: str
    node_id: str
    path: str
    mode: str
    owner: str
    started_s: float
    params: dict[str, Any] = field(default_factory=dict)
    _stuck: Any = None
    _stuck_set: bool = False

    def expired(self, now_s: float) -> bool:
        exp = self.params.get("expires_s")
        return exp is not None and now_s - self.started_s >= float(exp)

    def apply(self, true_value: Any, now_s: float, rng: random.Random, node_truth: dict[str, Any]) -> Any:
        p = self.params
        t = max(0.0, now_s - self.started_s)
        mode = self.mode
        if mode == "null":
            return None
        if mode == "random_null":
            return None if rng.random() < float(p.get("p", 0.3)) else true_value
        if mode == "stuck":
            if not self._stuck_set:
                self._stuck = p["value"] if "value" in p else copy.deepcopy(true_value)
                self._stuck_set = True
            return copy.deepcopy(self._stuck)
        if mode == "func":
            return p["fn"](true_value, t, node_truth)
        if mode == "const":
            value = p["value"]
            if isinstance(value, (int, float)) and not isinstance(value, bool) and p.get("noise"):
                value = value + rng.gauss(0.0, float(p["noise"]))
            return self._round(value)
        if not isinstance(true_value, (int, float)) or isinstance(true_value, bool):
            return true_value  # числовые подмены к нечисловым значениям не применяются
        if mode == "offset":
            return self._round(true_value + float(p["value"]))
        if mode == "mult":
            return self._round(true_value * float(p["value"]))
        if mode == "ramp_mult":
            k = float(p.get("start", 1.0)) + (float(p["end"]) - float(p.get("start", 1.0))) \
                * min(1.0, t / max(float(p["duration_s"]), 1e-6))
            return self._round(true_value * k)
        if mode == "ramp_to":
            k = min(1.0, t / max(float(p["duration_s"]), 1e-6))
            return self._round(true_value + (float(p["target"]) - true_value) * k)
        if mode == "noise":
            return self._round(true_value + rng.gauss(0.0, float(p["sigma"])))
        raise ValueError(f"Неизвестный режим подмены {mode!r}")

    def _round(self, value: Any) -> Any:
        digits = self.params.get("round")
        if digits is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = round(value, int(digits))
            if int(digits) == 0:
                value = int(value)
        return value

    def describe(self) -> dict[str, Any]:
        params = {k: v for k, v in self.params.items() if k != "fn"}
        return {"id": self.id, "node_id": self.node_id, "path": self.path, "mode": self.mode,
                "owner": self.owner, "started_s": round(self.started_s, 1), "params": params}


@dataclass
class Freeze:
    owner: str
    status: str = "UNKNOWN"
    block_commands: bool = True
    overrides: dict[str, Any] = field(default_factory=dict)  # например online=False


class TelemetryLayer:
    def __init__(self, store: StateStore, registry: NodeRegistry, env: Environment,
                 seed: Optional[int] = None) -> None:
        self.store = store
        self.registry = registry
        self.env = env
        self.rng = random.Random(seed)
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self.distortions: dict[str, Distortion] = {}
        self.freezes: dict[str, dict[str, Freeze]] = {}   # node_id -> owner -> Freeze
        self.published: dict[str, dict[str, Any]] = {}
        self.updated_at: dict[str, float] = {}

    def reset(self) -> None:
        with self._lock:
            self.distortions.clear()
            self.freezes.clear()
            self.published.clear()
            self.updated_at.clear()

    # ---------------- подмены ----------------

    def add_distortion(self, node_id: str, path: str, mode: str, owner: str = "manual",
                       **params: Any) -> str:
        parse_path(path)
        with self._lock:
            did = f"D{next(self._ids)}"
            self.distortions[did] = Distortion(did, node_id, path, mode, owner, self.env.sim_time_s, params)
            return did

    def remove_distortion(self, distortion_id: str) -> None:
        with self._lock:
            self.distortions.pop(distortion_id, None)

    def remove_owner(self, owner: str) -> None:
        """Снимает все подмены и заморозки, поставленные владельцем (кризисом)."""
        with self._lock:
            for did in [d.id for d in self.distortions.values() if d.owner == owner]:
                del self.distortions[did]
            for node_id in list(self.freezes):
                self.freezes[node_id].pop(owner, None)
                if not self.freezes[node_id]:
                    del self.freezes[node_id]

    def distortions_of(self, owner: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            return [d.describe() for d in self.distortions.values() if owner is None or d.owner == owner]

    # ---------------- заморозки ----------------

    def freeze_node(self, node_id: str, owner: str, status: str = "UNKNOWN",
                    block_commands: bool = True, **overrides: Any) -> None:
        with self._lock:
            self.freezes.setdefault(node_id, {})[owner] = Freeze(owner, status, block_commands, overrides)

    def unfreeze_node(self, node_id: str, owner: str) -> None:
        with self._lock:
            owners = self.freezes.get(node_id)
            if owners:
                owners.pop(owner, None)
                if not owners:
                    del self.freezes[node_id]

    def _system_freezes(self, all_nodes: dict[str, dict[str, Any]]) -> dict[str, Freeze]:
        """Заморозки, следующие из состояния инфраструктуры (канал, щит, edge)."""
        result: dict[str, Freeze] = {}
        edge = all_nodes.get(EDGE_NODE)
        if edge is not None:
            collector = edge.get("services", {}).get("telemetry_collector")
            if edge.get("state") == "OFFLINE" or collector != "RUNNING":
                freeze = Freeze("edge_collector", "UNKNOWN", block_commands=False)
                for node_id in EDGE_SERVED_NODES:
                    result[node_id] = freeze
        net = all_nodes.get(NETWORK_NODE)
        if net is not None and net.get("panel_link_state") == "DOWN":
            result[PANEL_NODE] = Freeze("panel_link", "UNKNOWN", block_commands=True, overrides={"online": False})
        return result

    def link_down(self) -> bool:
        try:
            return self.store.get(NETWORK_NODE, "link_state") == "DOWN"
        except KeyError:
            return False

    def agent_command_block(self, node_id: str) -> Optional[str]:
        """Причина, по которой команда агента узлу не пройдёт (None — пройдёт)."""
        if self.link_down():
            return "Нет связи с куполом: основной канал недоступен"
        with self._lock:
            freezes = dict(self.freezes.get(node_id, {}))
        for f in freezes.values():
            if f.block_commands:
                return f"Нет связи с узлом {node_id} (телеметрия потеряна)"
        if node_id == PANEL_NODE:
            try:
                if self.store.get(NETWORK_NODE, "panel_link_state") == "DOWN":
                    return "Нет связи с умным щитом (патчкорд)"
            except KeyError:
                pass
        return None

    def is_frozen(self, node_id: str) -> bool:
        with self._lock:
            if node_id in self.freezes:
                return True
        return node_id in self._system_freezes(self.store.get_all()) or self.link_down()

    # ---------------- построение вида ----------------

    def _sensor_nulls(self, node_id: str, view: dict[str, Any]) -> None:
        try:
            node = self.registry.get(node_id)
        except KeyError:
            return
        if isinstance(node, SensorNode) and view.get("sensor_state") == "FAULT":
            for key in getattr(node, "measured_keys", ()):
                if key not in SENSOR_META_FIELDS and key in view:
                    view[key] = None
            if "data_available" in view:
                view["data_available"] = False

    def _apply_distortions(self, node_id: str, view: dict[str, Any], truth: dict[str, Any],
                           now_s: float) -> None:
        for d in list(self.distortions.values()):
            if d.node_id != node_id:
                continue
            if d.expired(now_s):
                self.distortions.pop(d.id, None)
                continue
            true_value = get_path(truth, d.path)
            set_path(view, d.path, d.apply(true_value, now_s, self.rng, truth))

    def build_view(self, node_id: str, truth: dict[str, Any]) -> dict[str, Any]:
        """Живая телеметрия узла: подмены применены, заморозки — нет."""
        view = {k: copy.deepcopy(v) for k, v in truth.items()
                if not k.startswith(HIDDEN_PREFIX) and k != "control_override_source"}
        self._sensor_nulls(node_id, view)
        with self._lock:
            self._apply_distortions(node_id, view, truth, self.env.sim_time_s)
        return view

    def live_view(self, node_id: str) -> dict[str, Any]:
        return self.build_view(node_id, self.store.get_node(node_id))

    def live_value(self, node_id: str, path: str, default: Any = None) -> Any:
        return get_path(self.live_view(node_id), path, default)

    def publish(self) -> None:
        """Вызывается в конце каждого тика: формирует телеметрию, которую видит агент."""
        all_nodes = self.store.get_all()
        now = self.env.now()
        clock_offset = float(all_nodes.get(EDGE_NODE, {}).get("clock_offset_s") or 0.0)
        system = self._system_freezes(all_nodes)
        link_down = all_nodes.get(NETWORK_NODE, {}).get("link_state") == "DOWN"
        with self._lock:
            for node_id, truth in all_nodes.items():
                freezes = list(self.freezes.get(node_id, {}).values())
                if node_id in system:
                    freezes.append(system[node_id])
                if link_down:
                    freezes.append(Freeze("network_link", "UNKNOWN"))
                if freezes and node_id in self.published:
                    frozen = self.published[node_id]
                    frozen["status"] = freezes[0].status
                    for f in freezes:
                        frozen.update(f.overrides)
                    continue
                view = self.build_view(node_id, truth)
                stamp = now + (clock_offset if node_id in EDGE_SERVED_NODES else 0.0)
                self.updated_at[node_id] = stamp
                view["updated_at"] = round(stamp, 3)
                view["status"] = "OK"
                for f in freezes:  # узел заморожен ещё до первой публикации
                    view["status"] = f.status
                    view.update(f.overrides)
                self.published[node_id] = view
            for node_id in [n for n in self.published if n not in all_nodes]:
                del self.published[node_id]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            if not self.published:
                self.publish()
            return copy.deepcopy(self.published)

    def node_view(self, node_id: str) -> dict[str, Any]:
        with self._lock:
            if node_id not in self.published:
                self.publish()
            return copy.deepcopy(self.published[node_id])

    def describe(self) -> dict[str, Any]:
        with self._lock:
            return {
                "distortions": [d.describe() for d in self.distortions.values()],
                "freezes": {n: sorted(f) for n, f in self.freezes.items()},
                "link_down": self.link_down(),
            }


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

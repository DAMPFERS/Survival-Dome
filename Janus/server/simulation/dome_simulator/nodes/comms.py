# dome_simulator/nodes/comms.py
"""
Связь и вычисления (раздел 9 реестра dome_sandbox_nodes.md):
radio, backup_comms, edge_compute.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from ..environment import OUProcess, clamp, relax
from ..events import Severity
from .base import register_node_type
from .sandbox import ControlError, SandboxNode, control, parse_choice, parse_number

# ---------------------------------------------------------------------------
# 9.1 Радиосвязь
# ---------------------------------------------------------------------------

# протокол: (макс. скорость, кбит/с; минимальный SNR для приёма, дБ)
RADIO_PROTOCOLS = {
    "AX25": (1.2, 6.0),
    "FSK": (9.6, 10.0),
    "LORA": (5.5, -12.0),
    "DMR": (4.8, 8.0),
}
RADIO_BAND_MHZ = (430.0, 440.0)


@register_node_type("radio")
class Radio(SandboxNode):
    """
    Шум эфира зависит от погоды (осадки, ветер) и от «грязных» участков
    диапазона: вокруг interference_mhz — сильные помехи, их центр медленно
    смещается. Смена частоты/протокола — способ восстановить связь.
    """
    title = "Радиосвязь"
    system = "Внешние системы"
    category = "comms"

    SIGNAL_DBM = -105.0  # уровень полезного сигнала удалённого корреспондента

    def __init__(self, node_id: str, frequency_mhz: float = 433.5, protocol: str = "FSK",
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.frequency_mhz = frequency_mhz
        self.protocol = protocol
        self._interference_center = OUProcess(436.0, 2.5, 6 * 3600, self.rng, start=437.0)
        self._fading = OUProcess(0.0, 3.0, 600, self.rng)

    def initial_state(self) -> dict[str, Any]:
        return {
            "frequency_mhz": self.frequency_mhz,
            "protocol": self.protocol,
            "bitrate_kbps": 0.0,
            "noise_level_dbm": -120.0,
            "snr_db": 0.0,
            "signal_quality_pct": 0.0,
            "reception": False,
            "state": "NO_SIGNAL",          # RECEIVING | NO_SIGNAL
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        o = self.env.outdoor
        center = clamp(self._interference_center.step(gdt), *RADIO_BAND_MHZ)
        distance = abs(node["frequency_mhz"] - center)
        interference_db = 25.0 * math.exp(-(distance / 0.6) ** 2)
        weather_db = 1.5 * min(o.precipitation_mm_h, 8.0) + 0.3 * max(0.0, o.wind_speed_ms - 10.0)
        noise_dbm = -120.0 + interference_db + weather_db + self.noise(1.0)

        snr = self.SIGNAL_DBM + self._fading.step(gdt) - noise_dbm
        max_rate, min_snr = RADIO_PROTOCOLS[node["protocol"]]
        margin = snr - min_snr
        quality = clamp(50.0 + margin * 5.0, 0.0, 100.0)
        reception = margin > 0
        bitrate = max_rate * clamp(margin / 10.0, 0.0, 1.0) if reception else 0.0

        if node["reception"] and not reception:
            self.emit("radio.signal_lost", Severity.WARNING, frequency_mhz=node["frequency_mhz"],
                      noise_level_dbm=round(noise_dbm, 1))
        return {
            "bitrate_kbps": round(bitrate, 2),
            "noise_level_dbm": round(noise_dbm, 1),
            "snr_db": round(snr, 1),
            "signal_quality_pct": round(quality, 1),
            "reception": reception,
            "state": "RECEIVING" if reception else "NO_SIGNAL",
        }

    @control("set_frequency")
    def _set_frequency(self, node, value):
        return {"frequency_mhz": round(parse_number(value, *RADIO_BAND_MHZ, "Частота, МГц"), 3)}

    @control("set_protocol")
    def _set_protocol(self, node, value):
        return {"protocol": parse_choice(value, tuple(RADIO_PROTOCOLS), "Протокол")}


# ---------------------------------------------------------------------------
# 9.2 Резервный канал связи
# ---------------------------------------------------------------------------

# канал: (базовая задержка мс, макс. скорость кбит/с, допустимые протоколы)
BACKUP_CHANNELS = {
    "SATELLITE": (650.0, 64.0, ("IRIDIUM_SBD", "IP")),
    "LORA": (1500.0, 5.5, ("LORAWAN", "MESHTASTIC")),
    "MESH": (60.0, 1000.0, ("BATMAN", "OLSR")),
}
TRAFFIC_PRIORITIES = ("NORMAL", "TELEMETRY", "EMERGENCY")


@register_node_type("backup_comms")
class BackupComms(SandboxNode):
    """
    Каждый канал деградирует по-своему: спутник — от облачности и осадков,
    LoRa — от помех, mesh — от случайных разрывов соседних узлов.
    Приоритет EMERGENCY уменьшает задержку ценой скорости.
    """
    title = "Резервный канал связи (спутник / LoRa / mesh)"
    system = "Внешние системы"
    category = "comms"

    def __init__(self, node_id: str, channel: str = "SATELLITE", seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.channel = channel
        self._quality = {ch: OUProcess(0.8, 0.12, 1800, self.rng) for ch in BACKUP_CHANNELS}
        self._mesh_outage_s = 0.0

    def initial_state(self) -> dict[str, Any]:
        return {
            "active_channel": self.channel,
            "protocol": BACKUP_CHANNELS[self.channel][2][0],
            "traffic_priority": "NORMAL",
            "available": True,
            "latency_ms": BACKUP_CHANNELS[self.channel][0],
            "signal_quality_pct": 80.0,
            "bitrate_kbps": 0.0,
            "state": "AVAILABLE",          # AVAILABLE | DEGRADED | UNAVAILABLE
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        o = self.env.outdoor
        channel = node["active_channel"]
        base_latency, max_rate, _ = BACKUP_CHANNELS[channel]

        qualities = {ch: proc.step(gdt) for ch, proc in self._quality.items()}
        quality = qualities[channel]
        if channel == "SATELLITE":
            quality -= 0.3 * o.cloud_cover ** 2 + 0.05 * min(o.precipitation_mm_h, 6.0)
        elif channel == "MESH":
            if self._mesh_outage_s > 0:
                self._mesh_outage_s -= gdt
                quality = 0.0
            elif self.happens(0.05, gdt):
                self._mesh_outage_s = self.rng.uniform(600.0, 3600.0)
        quality = clamp(quality, 0.0, 1.0)

        if quality < 0.15:
            state = "UNAVAILABLE"
        elif quality < 0.5:
            state = "DEGRADED"
        else:
            state = "AVAILABLE"
        if state != node["state"] and state != "AVAILABLE":
            self.emit("backup_comms.degraded", Severity.WARNING, channel=channel, state=state)

        priority = node["traffic_priority"]
        latency_k = {"NORMAL": 1.0, "TELEMETRY": 0.9, "EMERGENCY": 0.7}[priority]
        rate_k = {"NORMAL": 1.0, "TELEMETRY": 0.8, "EMERGENCY": 0.4}[priority]
        available = state != "UNAVAILABLE"
        latency = base_latency * latency_k * (1.0 + 2.0 * (1.0 - quality)) * (1.0 + abs(self.noise(0.05)))
        return {
            "available": available,
            "latency_ms": round(latency, 0) if available else None,
            "signal_quality_pct": round(quality * 100.0, 1),
            "bitrate_kbps": round(max_rate * rate_k * quality ** 2, 2) if available else 0.0,
            "state": state,
        }

    @control("switch_channel")
    def _switch_channel(self, node, value):
        channel = parse_choice(value, tuple(BACKUP_CHANNELS), "Канал")
        return {"active_channel": channel, "protocol": BACKUP_CHANNELS[channel][2][0]}

    @control("set_traffic_priority")
    def _set_priority(self, node, value):
        return {"traffic_priority": parse_choice(value, TRAFFIC_PRIORITIES, "Приоритет трафика")}

    @control("set_protocol")
    def _set_protocol(self, node, value):
        allowed = BACKUP_CHANNELS[node["active_channel"]][2]
        return {"protocol": parse_choice(value, allowed, f"Протокол канала {node['active_channel']}")}


# ---------------------------------------------------------------------------
# 9.3 Локальный вычислительный узел (edge)
# ---------------------------------------------------------------------------

EDGE_SERVICES = ("agent_runtime", "telemetry_collector", "vision_inference", "api_gateway")


@register_node_type("edge_compute")
class EdgeCompute(SandboxNode):
    """
    Нагрузка — по суточному профилю работы агентов + шум. Память медленно
    «утекает» до перезагрузки. Сервисы изредка падают (DEGRADED).
    Перегрев (>85 °C) — троттлинг и состояние OVERHEAT.
    """
    title = "Локальный вычислительный узел / edge-сервер"
    system = "Вычисления"
    category = "compute"

    REBOOT_S = 180.0

    def __init__(self, node_id: str, service_failure_rate_per_hour: float = 0.01,
                 seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.service_failure_rate_per_hour = service_failure_rate_per_hour
        self._cpu = OUProcess(0.0, 10.0, 600, self.rng)
        self._gpu = OUProcess(0.0, 15.0, 900, self.rng)
        self._reboot_remaining_s = 0.0
        self._restarting: dict[str, float] = {}

    def initial_state(self) -> dict[str, Any]:
        return {
            "cpu_load_pct": 20.0,
            "gpu_load_pct": 10.0,
            "free_memory_pct": 70.0,
            "temperature_c": 45.0,
            "services": {s: "RUNNING" for s in EDGE_SERVICES},
            "state": "ONLINE",             # ONLINE | DEGRADED | OFFLINE | OVERHEAT
            "load_limit_pct": 100.0,
            "uptime_h": 0.0,
            "control_powered": True,
        }

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        ambient = self.env.indoor.temperature_c
        if not node["control_powered"] or self._reboot_remaining_s > 0:
            if not node["control_powered"]:
                self._reboot_remaining_s = self.REBOOT_S  # после возврата питания — загрузка
            else:
                self._reboot_remaining_s -= gdt
            booting = node["control_powered"] and self._reboot_remaining_s <= 0
            return {
                "cpu_load_pct": 0.0, "gpu_load_pct": 0.0,
                "free_memory_pct": 85.0 if booting else node["free_memory_pct"],
                "temperature_c": round(relax(node["temperature_c"], ambient, 600, gdt), 1),
                "services": {s: ("RUNNING" if booting else "STOPPED") for s in EDGE_SERVICES},
                "state": "ONLINE" if booting else "OFFLINE",
                "uptime_h": 0.0,
            }

        services = dict(node["services"])
        for name, remaining in list(self._restarting.items()):
            remaining -= gdt
            if remaining <= 0:
                services[name] = "RUNNING"
                del self._restarting[name]
            else:
                self._restarting[name] = remaining
        if self.happens(self.service_failure_rate_per_hour, gdt):
            victim = self.rng.choice([s for s in EDGE_SERVICES if services[s] == "RUNNING"] or list(EDGE_SERVICES))
            services[victim] = "FAILED"
            self.emit("edge.service_failed", Severity.WARNING, service=victim)

        hour = self.hour
        demand = 55.0 if 8 <= hour < 22 else 25.0
        limit = node["load_limit_pct"]
        cpu = clamp(demand + self._cpu.step(gdt), 2.0, limit)
        gpu_demand = 45.0 if services["vision_inference"] == "RUNNING" else 0.0
        gpu = clamp(gpu_demand + self._gpu.step(gdt), 0.0, limit) if gpu_demand else 0.0

        heat = ambient + 0.35 * cpu + 0.3 * gpu + 10.0
        temp = relax(node["temperature_c"], heat, 600, gdt) + self.noise(0.3)
        throttled = temp > 85.0
        if throttled:
            cpu, gpu = cpu * 0.6, gpu * 0.6

        free_mem = clamp(node["free_memory_pct"] - 0.8 * gdt / 3600.0 + self.noise(0.3), 3.0, 95.0)

        if throttled:
            state = "OVERHEAT"
        elif any(v != "RUNNING" for v in services.values()) or free_mem < 10.0:
            state = "DEGRADED"
        else:
            state = "ONLINE"
        if state != node["state"] and state in ("OVERHEAT", "DEGRADED"):
            self.emit("edge.state", Severity.WARNING, state=state)

        return {
            "cpu_load_pct": round(cpu, 1),
            "gpu_load_pct": round(gpu, 1),
            "free_memory_pct": round(free_mem, 1),
            "temperature_c": round(temp, 1),
            "services": services,
            "state": state,
            "uptime_h": round(node["uptime_h"] + gdt / 3600.0, 2),
        }

    @control("restart_service")
    def _restart_service(self, node, value):
        service = value.strip().lower() if isinstance(value, str) else None
        if service not in EDGE_SERVICES:
            raise ControlError(f"Сервис: ожидалось одно из {list(EDGE_SERVICES)}, получено {value!r}")
        if node["state"] == "OFFLINE":
            raise ControlError("Узел offline")
        services = dict(node["services"])
        services[service] = "RESTARTING"
        self._restarting[service] = 60.0
        return {"services": services}

    @control("limit_load", "set_load_limit")
    def _limit_load(self, node, value):
        return {"load_limit_pct": parse_number(value, 10.0, 100.0, "Ограничение нагрузки, %")}

    @control("reboot")
    def _reboot(self, node, value):
        self._reboot_remaining_s = self.REBOOT_S
        self._restarting.clear()
        return {"state": "OFFLINE", "services": {s: "STOPPED" for s in EDGE_SERVICES},
                "cpu_load_pct": 0.0, "gpu_load_pct": 0.0}

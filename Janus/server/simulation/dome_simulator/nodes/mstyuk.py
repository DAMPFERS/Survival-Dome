# dome_simulator/nodes/mstyuk.py
"""
МС-ТЮК (раздел 10 реестра dome_sandbox_nodes.md): две теплицы-бокса.

    greenhouse_fablab  (thermal_insulation_01) — теплица ФабЛаб;
    greenhouse_chemlab (thermal_insulation_02) — теплица Химлаб (химреактор).

Модель повторяет HTTP API реальных стендов (TYC/fablab_client.py,
TYC/chem_reactor_client.py): те же датчики, индексы и команды — при переходе
узла в режим real телеметрия сохраняет форму.

«Снаружи» бокса — воздух купола (env.indoor), давление — атмосферное.
Соглашение об индексах датчиков: последние индексы — снаружи бокса
(ФабЛаб: BMP280/DHT11 0–1 внутри, 2 снаружи; SGP40 0 внутри, 1 снаружи;
Химлаб: BMP280/DHT11/MQ-2 0 внутри, 1 снаружи). Вентиляторы ФабЛаб 0–3 — вдув,
4–5 — выдув; Химлаб 0 — вдув, 1 — выдув.

Двери (концевики) открывает Оператор руками (set_door, physical) — только при
разблокированном магнитном замке.
"""
from __future__ import annotations

from typing import Any, Optional

from ..environment import OUProcess, clamp, relax
from ..events import Severity
from ..store import StateStore
from .base import register_node_type
from .sandbox import ControlError, SandboxNode, control, parse_bool, parse_number

OPEN, CLOSED = "OPEN", "CLOSED"
LOCKED, UNLOCKED = "LOCKED", "UNLOCKED"
ADC_MAX, ADC_VREF = 1023, 5.0


def _adc_voltage(adc: float) -> float:
    return round(adc / ADC_MAX * ADC_VREF, 3)


def _index(value: Any, key: str, count: int, what: str) -> int:
    if not isinstance(value, dict):
        raise ControlError(f'{what}: ожидался объект с полем "{key}" (0..{count - 1})')
    return int(parse_number(value.get(key), 0, count - 1, what))


class _Greenhouse(SandboxNode):
    """Общая физика бокса: вентиляторы с ШИМ, воздухообмен с куполом, двери и замки."""
    system = "МС-ТЮК"
    category = "climate"

    FANS = 2
    FANS_IN = 1               # первые FANS_IN вентиляторов — вдув, остальные — выдув
    DOORS = 2
    MAX_RPM = 3000.0
    MIN_SPIN_PWM = 30         # ниже — вентилятор не стартует

    def __init__(self, node_id: str, internal_heat_c: float = 6.0, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.internal_heat_c = internal_heat_c

    def _fan_state(self) -> dict[str, Any]:
        return {"fans_pwm": [128] * self.FANS, "fans_rpm": [0] * self.FANS,
                "doors": [CLOSED] * self.DOORS}

    def _rpm(self, pwm: int, current: float, gdt: float) -> float:
        target = 0.0 if pwm < self.MIN_SPIN_PWM else self.MAX_RPM * pwm / 255.0
        rpm = relax(current, target, 3, gdt)
        return max(0.0, rpm + self.noise(12.0)) if rpm > 50 else 0.0

    def _airflow(self, pwm: list[int], node: dict[str, Any], gdt: float) -> tuple[list[int], float, float]:
        """Обороты и доли потока вдува/выдува (0..1)."""
        rpm = [round(self._rpm(p, r, gdt)) for p, r in zip(pwm, node["fans_rpm"])]
        n_in, n_out = self.FANS_IN, self.FANS - self.FANS_IN
        flow_in = sum(rpm[:n_in]) / (self.MAX_RPM * n_in)
        flow_out = sum(rpm[n_in:]) / (self.MAX_RPM * n_out) if n_out else 0.0
        return rpm, flow_in, flow_out

    @staticmethod
    def _doors_open(node: dict[str, Any]) -> bool:
        return OPEN in node["doors"]

    def _set_pwm(self, node: dict[str, Any], value: Any) -> tuple[int, int]:
        fan = _index(value, "fan", self.FANS, "Индекс вентилятора")
        pwm = int(parse_number(value.get("pwm"), 0, 255, "PWM"))
        return fan, pwm

    def _locked(self, node: dict[str, Any], door: int) -> bool:
        raise NotImplementedError

    @control("set_fan_pwm")
    def _set_fan_pwm(self, node, value):
        """value: {"fan": индекс, "pwm": 0..255}."""
        fan, pwm = self._set_pwm(node, value)
        fans = list(node["fans_pwm"])
        fans[fan] = pwm
        return {"fans_pwm": fans}

    @control("set_door", physical=True)
    def _set_door(self, node, value):
        """Оператор открывает/закрывает дверь бокса: {"door": 0..1, "open": true|false}."""
        door = _index(value, "door", self.DOORS, "Индекс двери")
        want_open = parse_bool(value.get("open"), True)
        if want_open and self._locked(node, door):
            raise ControlError(f"Дверь {door} заблокирована магнитным замком — сначала разблокируйте его")
        doors = list(node["doors"])
        doors[door] = OPEN if want_open else CLOSED
        return {"doors": doors}


# ---------------------------------------------------------------------------
# 10.1 Теплица ФабЛаб
# ---------------------------------------------------------------------------

@register_node_type("greenhouse_fablab")
class GreenhouseFabLab(_Greenhouse):
    """
    6 вентиляторов (0–3 вдув, 4–5 выдув), BMP280 ×3 (температура, давление),
    DHT11 ×3 (влажность), SGP40 ×2 (VOC Index), датчик пыли GP2Y1010AU0F
    (ADC и напряжение), концевики дверей ×2, магнитные замки ×2, пылесос.

    Режим неисправности (как admin/mode реального стенда, для админа):
        fault_mode 0 — выкл; 1 — вентилятору fault_fan каждые 30 с выставляется
        случайный PWM; 2 — команда set_fan_pwm для fault_fan искажается случайным PWM.

    Шина опроса (Д16): crc_errors, poll_interval_ms, set_poll_interval.
    Кризисный вход control_bus_collision — конфликт адресов (№22): при опросе
    чаще 500 мс 20–40 % значений приходят пустыми (null), растут CRC-ошибки.
    """
    title = "Теплица ФабЛаб"

    FANS, FANS_IN = 6, 4
    BMP, DHT, VOC = 3, 3, 2
    FAULT_PERIOD_S = 30.0

    def __init__(self, node_id: str, internal_heat_c: float = 8.0, seed: Optional[int] = None) -> None:
        super().__init__(node_id, internal_heat_c, seed)
        self._voc_source = OUProcess(40.0, 15.0, 1800, self.rng)
        self._dust_source = OUProcess(0.12, 0.06, 1200, self.rng)   # пыль от работ, мг/м³
        self._fault_timer = 0.0

    def initial_state(self) -> dict[str, Any]:
        base_pa = round(self.env.outdoor.pressure_hpa * 100.0, 1)
        return {
            **self._fan_state(),
            "temperature_c": [24.0, 24.0, 21.0],
            "pressure_pa": [base_pa] * self.BMP,
            "humidity_pct": [45.0, 45.0, 45.0],
            "voc_index": [120, 100],
            "dust_adc": 150,
            "dust_voltage_v": _adc_voltage(150),
            "locks": [LOCKED] * self.DOORS,
            "vacuum_on": False,
            "fault_mode": 0,
            "fault_fan": 0,
            "poll_interval_ms": 200,
            "crc_errors": 0,
            "control_bus_collision": False,
        }

    def _locked(self, node, door):
        return node["locks"][door] == LOCKED

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        pwm = list(node["fans_pwm"])
        if node["fault_mode"] == 1:
            self._fault_timer += gdt
            if self._fault_timer >= self.FAULT_PERIOD_S:
                self._fault_timer = 0.0
                pwm[node["fault_fan"]] = self.rng.randint(0, 255)
        rpm, flow_in, flow_out = self._airflow(pwm, node, gdt)

        indoor = self.env.indoor
        outside_t, outside_h, outside_voc = indoor.temperature_c, indoor.humidity_pct, indoor.voc_index
        outside_pa = self.env.outdoor.pressure_hpa * 100.0
        doors_open = self._doors_open(node)
        exchange = 0.05 + flow_in + flow_out + (3.0 if doors_open else 0.0)

        t_target = outside_t + self.internal_heat_c / (1.0 + 4.0 * exchange)
        t_in = relax(node["temperature_c"][0], t_target, 1200 / exchange, gdt)
        h_in = relax(node["humidity_pct"][0], outside_h + 6.0 / (1.0 + 4.0 * exchange), 1200 / exchange, gdt)
        voc_target = outside_voc + self._voc_source.step(gdt) / (1.0 + 4.0 * exchange)
        voc_in = relax(node["voc_index"][0], voc_target, 900 / exchange, gdt)
        overpressure = 0.0 if doors_open else 35.0 * (flow_in - 0.8 * flow_out)

        # Пыль: источник — работы в боксе; пылесос убирает 70 %, вентиляция разбавляет
        dust_target = max(0.0, self._dust_source.step(gdt)) * (0.3 if node["vacuum_on"] else 1.0) \
            / (1.0 + 2.0 * exchange)
        dust_now = max(0.0, (node["dust_voltage_v"] * 0.17) - 0.1)       # формула Sharp: мг/м³ из В
        dust = relax(dust_now, dust_target, 300.0, gdt)
        dust_adc = int(clamp(round((dust + 0.1) / 0.17 / ADC_VREF * ADC_MAX + self.noise(4.0)), 0, ADC_MAX))

        u: dict[str, Any] = {
            "fans_pwm": pwm,
            "fans_rpm": rpm,
            "temperature_c": [round(t_in + self.noise(0.05), 2), round(t_in + 0.3 + self.noise(0.05), 2),
                              round(outside_t + self.noise(0.05), 2)],
            "pressure_pa": [round(outside_pa + overpressure + self.noise(0.8), 1) for _ in range(2)]
                           + [round(outside_pa + self.noise(0.8), 1)],
            "humidity_pct": [round(clamp(h_in + self.noise(0.5), 0, 100)), round(clamp(h_in + self.noise(0.5), 0, 100)),
                             round(clamp(outside_h + self.noise(0.5), 0, 100))],   # DHT11 — целые %
            "voc_index": [int(clamp(voc_in + self.noise(2.0), 1, 500)), int(clamp(outside_voc + self.noise(2.0), 1, 500))],
            "dust_adc": dust_adc,
            "dust_voltage_v": _adc_voltage(dust_adc),
        }
        if self.bus_collision_active(node):
            # конфликт адресов: часть ответов не проходит проверку CRC
            # (пропуски значений в телеметрии добавляет слой телеметрии, см. кризис №22)
            loss = self.rng.uniform(0.2, 0.4)
            polls = gdt * 1000.0 / max(node["poll_interval_ms"], 1)
            u["crc_errors"] = int(node["crc_errors"] + max(1, round(polls * loss * 0.05)))
        return u

    @staticmethod
    def bus_collision_active(node: dict[str, Any]) -> bool:
        return bool(node.get("control_bus_collision")) and node.get("poll_interval_ms", 0) < 500

    def real_command(self, store: StateStore, action: str, value: Any = None) -> list[tuple[str, Any]]:
        if action == "set_poll_interval":   # период опроса шины — настройка сервера, не стенда
            store.update(self.node_id, **self._set_poll_interval(store.get_node(self.node_id), value))
            return []
        return [(action, value)]

    # ---- управление ----

    @control("set_poll_interval")
    def _set_poll_interval(self, node, value):
        """Период опроса шины, мс (50..5000)."""
        return {"poll_interval_ms": int(parse_number(value, 50, 5000, "Период опроса, мс"))}

    @control("set_fan_pwm")
    def _set_fan_pwm(self, node, value):
        """value: {"fan": 0..5, "pwm": 0..255}."""
        fan, pwm = self._set_pwm(node, value)
        if node["fault_mode"] == 2 and fan == node["fault_fan"]:
            pwm = self.rng.randint(0, 255)
        fans = list(node["fans_pwm"])
        fans[fan] = pwm
        return {"fans_pwm": fans}

    @control("set_lock", "lock")
    def _set_lock(self, node, value):
        """value: {"lock": 0..1, "locked": true|false}."""
        idx = _index(value, "lock", self.DOORS, "Индекс замка")
        locks = list(node["locks"])
        locks[idx] = LOCKED if parse_bool(value.get("locked"), True) else UNLOCKED
        return {"locks": locks}

    @control("set_vacuum", "vacuum")
    def _set_vacuum(self, node, value):
        """value: 1/0 (true/false) или {"mode": 1|0}."""
        mode = value.get("mode") if isinstance(value, dict) else value
        if mode is None:
            raise ControlError("set_vacuum: ожидалось 1 (включить) или 0 (выключить)")
        on = parse_bool(mode)
        if on != node["vacuum_on"]:
            self.emit("greenhouse.vacuum", Severity.INFO, vacuum_on=on)
        return {"vacuum_on": on}

    @control("set_fault_mode")
    def _set_fault_mode(self, node, value):
        """value: {"mode": 0..2, "fan": 0..5} — режим неисправности стенда (для админа)."""
        if not isinstance(value, dict):
            raise ControlError('set_fault_mode: ожидалось {"mode": 0..2, "fan": 0..5}')
        mode = int(parse_number(value.get("mode"), 0, 2, "Режим неисправности"))
        fan = int(parse_number(value.get("fan", node["fault_fan"]), 0, self.FANS - 1, "Индекс вентилятора"))
        self._fault_timer = 0.0
        return {"fault_mode": mode, "fault_fan": fan}


# ---------------------------------------------------------------------------
# 10.2 Теплица Химлаб
# ---------------------------------------------------------------------------

@register_node_type("greenhouse_chemlab")
class GreenhouseChemLab(_Greenhouse):
    """
    2 вентилятора Arduino (0 вдув, 1 выдув), BMP280 ×2, DHT11 ×2, газ MQ-2 ×2
    (ADC 0..1023 и напряжение — это НЕ VOC Index), концевики дверей ×2,
    общий магнитный замок (оба замка сразу), релейный вентилятор lmp8:
    уровень 0..10 — номер комбинации реле (производительность ≈ уровень/10).

    Источник газа — реакция в боксе; вытяжка (вентиляторы, релейный вентилятор,
    открытые двери) разбавляет газ воздухом купола.
    """
    title = "Теплица Химлаб"

    FANS, FANS_IN = 2, 1
    RELAY_LEVELS = 10
    GAS_CLEAN_ADC = 120.0     # MQ-2 на чистом воздухе после прогрева

    def __init__(self, node_id: str, internal_heat_c: float = 5.0, seed: Optional[int] = None) -> None:
        super().__init__(node_id, internal_heat_c, seed)
        self._reaction = OUProcess(180.0, 80.0, 1500, self.rng)   # выделение газа, ед. ADC над фоном

    def initial_state(self) -> dict[str, Any]:
        base_pa = round(self.env.outdoor.pressure_hpa * 100.0, 1)
        adc = [int(self.GAS_CLEAN_ADC) + 60, int(self.GAS_CLEAN_ADC)]
        return {
            **self._fan_state(),
            "temperature_c": [24.0, 21.0],
            "pressure_pa": [base_pa, base_pa],
            "humidity_pct": [50.0, 45.0],
            "gas_adc": adc,
            "gas_voltage_v": [_adc_voltage(a) for a in adc],
            "lock": LOCKED,
            "relay_fan_level": 0,
        }

    def _locked(self, node, door):
        return node["lock"] == LOCKED

    def simulate(self, gdt: float, node: dict[str, Any]) -> dict[str, Any]:
        rpm, flow_in, flow_out = self._airflow(list(node["fans_pwm"]), node, gdt)
        relay = node["relay_fan_level"] / self.RELAY_LEVELS
        indoor = self.env.indoor
        outside_t, outside_h = indoor.temperature_c, indoor.humidity_pct
        outside_pa = self.env.outdoor.pressure_hpa * 100.0
        doors_open = self._doors_open(node)
        exchange = 0.05 + flow_in + flow_out + 1.5 * relay + (3.0 if doors_open else 0.0)

        t_target = outside_t + self.internal_heat_c / (1.0 + 4.0 * exchange)
        t_in = relax(node["temperature_c"][0], t_target, 1200 / exchange, gdt)
        h_in = relax(node["humidity_pct"][0], outside_h + 10.0 / (1.0 + 4.0 * exchange), 1200 / exchange, gdt)
        gas_outside = self.GAS_CLEAN_ADC + 400.0 * indoor.chem_ppm
        gas_target = gas_outside + max(0.0, self._reaction.step(gdt)) / (1.0 + 4.0 * exchange)
        gas_in = relax(node["gas_adc"][0], gas_target, 600 / exchange, gdt)
        overpressure = 0.0 if doors_open else 30.0 * (flow_in - 0.8 * flow_out - 0.8 * relay)

        gas = [int(clamp(round(gas_in + self.noise(3.0)), 0, ADC_MAX)),
               int(clamp(round(gas_outside + self.noise(3.0)), 0, ADC_MAX))]
        return {
            "fans_rpm": rpm,
            "temperature_c": [round(t_in + self.noise(0.05), 2), round(outside_t + self.noise(0.05), 2)],
            "pressure_pa": [round(outside_pa + overpressure + self.noise(0.8), 1),
                            round(outside_pa + self.noise(0.8), 1)],
            "humidity_pct": [round(clamp(h_in + self.noise(0.5), 0, 100)), round(clamp(outside_h + self.noise(0.5), 0, 100))],
            "gas_adc": gas,
            "gas_voltage_v": [_adc_voltage(a) for a in gas],
        }

    # ---- управление ----

    @control("set_relay_fan_level", "set_relay_level")
    def _set_relay_level(self, node, value):
        """value: уровень релейного вентилятора lmp8 0..10 (или {"level": ...})."""
        level = value.get("level") if isinstance(value, dict) else value
        return {"relay_fan_level": int(parse_number(level, 0, self.RELAY_LEVELS, "Уровень релейного вентилятора"))}

    @control("set_lock", "lock")
    def _set_lock(self, node, value):
        """value: true/false (или {"locked": ...}) — общий замок, оба замка сразу."""
        locked = value.get("locked") if isinstance(value, dict) else value
        if locked is None:
            raise ControlError("set_lock: ожидалось true (заблокировать) или false (разблокировать)")
        return {"lock": LOCKED if parse_bool(locked) else UNLOCKED}

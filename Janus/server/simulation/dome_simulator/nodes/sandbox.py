# dome_simulator/nodes/sandbox.py
"""
Общая основа узлов песочницы (реестр — dome_sandbox_nodes.md).

SandboxNode берёт на себя всё, что одинаково у всех устройств:
    - общие поля телеметрии: online, control_override_source;
    - пропуск виртуальной логики, когда узел питается реальным железом
      (control_override_source == "real");
    - перевод dt симуляции в игровые секунды (см. environment.py);
    - сбор событий за тик (self.emit);
    - реестр управляющих воздействий: методы, помеченные @control(...).

Подкласс реализует:
    initial_state() -> dict      — стартовая телеметрия;
    simulate(gdt, node) -> dict  — генерация данных за шаг gdt (игровые секунды);
                                   node — копия текущего состояния, вернуть обновления;
    @control("имя", "алиас") def ...(self, node, value) -> dict | None.

Скрытое физическое состояние, которого нет в телеметрии (фаза шумового
процесса, «истинный» износ и т.п.), хранится в атрибутах инстанса.
Всё, что должно быть видно или переживать save/load snapshot, — в StateStore.

Поля control_* — входы для правил зависимостей и кризисов: узел читает их,
но сам не меняет (кроме сброса аварий собственными командами, например reset_error).

Время: simulate() получает шаг в секундах времени УСТРОЙСТВА (env.device_dt) —
при гибридной модели это реальное время 1:1, суточный цикл берётся из self.hour.

Физические действия (@control(..., physical=True)) выполняет Оператор руками в
куполе (замена патчкорда, чистка фильтра, осмотр кабеля). ИИ-агенту они
недоступны — фасад Simulator.control отклоняет их при origin="agent".
"""
from __future__ import annotations

import random
from abc import abstractmethod
from typing import Any, Callable, Optional

from ..environment import Environment, clamp, poisson_happens
from ..events import Event, EventBus, Severity
from ..store import StateStore
from .base import BaseNode


class ControlError(ValueError):
    """Недопустимое управляющее воздействие (неизвестное действие или значение)."""


def control(*names: str, physical: bool = False) -> Callable[[Callable], Callable]:
    """Помечает метод узла как обработчик управляющих воздействий с именами names.
    Имена сравниваются без учёта регистра: START_CELL_BALANCING == start_cell_balancing.
    physical=True — действие выполняет Оператор физически (агенту недоступно)."""
    def decorator(fn: Callable) -> Callable:
        fn._control_names = tuple(n.lower() for n in names)
        fn._control_physical = physical
        return fn
    return decorator


class SandboxNode(BaseNode):
    title: str = ""          # человекочитаемое название из реестра
    system: str = ""         # «Энергетика», «Климат», ...

    _controls: dict[str, str] = {}             # любое имя (включая алиасы) -> метод
    _control_aliases: dict[str, list[str]] = {}  # основное имя -> алиасы
    _physical: frozenset = frozenset()           # основные имена физических действий Оператора

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        controls: dict[str, str] = {}
        methods: dict[str, tuple[str, ...]] = {}
        physical: dict[str, bool] = {}
        for klass in reversed(cls.__mro__):
            for attr, fn in vars(klass).items():
                names = getattr(fn, "_control_names", ())
                if names:
                    methods[attr] = names  # переопределение в подклассе заменяет набор имён
                    physical[attr] = getattr(fn, "_control_physical", False)
        for attr, names in methods.items():
            for name in names:
                controls[name] = attr
        cls._controls = controls
        cls._control_aliases = {names[0]: list(names[1:]) for names in methods.values()}
        cls._physical = frozenset(names[0] for attr, names in methods.items() if physical[attr])

    def __init__(self, node_id: str, seed: Optional[int] = None) -> None:
        super().__init__(node_id)
        self.rng = random.Random(seed)
        self.env: Environment = Environment()  # заменяется общим через bind_environment
        self._events: list[Event] = []

    def bind_environment(self, env: Environment) -> None:
        self.env = env

    # ---------------- контракт BaseNode ----------------

    def get_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"online": True}
        initial = self.initial_state()
        self.measured_keys = tuple(k for k in initial if not k.startswith("control_"))
        state.update(initial)
        state["control_override_source"] = None  # None | "real" | "virtual"
        return state

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        if dt <= 0:
            return []
        node = state.get_node(self.node_id)
        if node.get("control_override_source") == "real":
            return []
        updates = self.simulate(self.env.device_dt(dt), node)
        if updates:
            state.update(self.node_id, **updates)
        return self._drain_events()

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        raise NotImplementedError("Узлы песочницы управляются через Simulator.control()")

    # ---------------- для подклассов ----------------

    @abstractmethod
    def initial_state(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def simulate(self, gdt: float, node: dict[str, Any]) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def emit(self, event_type: str, severity: Severity = Severity.INFO, **payload: Any) -> None:
        self._events.append(Event(type=event_type, source=self.node_id,
                                  payload=payload, severity=severity, timestamp=self.env.now()))

    def noise(self, sigma: float) -> float:
        return self.rng.gauss(0.0, sigma)

    def happens(self, rate_per_hour: float, gdt: float) -> bool:
        return poisson_happens(rate_per_hour, gdt, self.rng)

    @property
    def hour(self) -> float:
        return self.env.outdoor.hour

    # ---------------- управление ----------------

    @classmethod
    def available_controls(cls) -> list[str]:
        """Основные имена управляющих воздействий (без алиасов)."""
        return sorted(cls._control_aliases)

    @classmethod
    def canonical_control(cls, action: str) -> Optional[str]:
        """Основное имя действия по любому его имени/алиасу; None — действие не поддерживается."""
        method = cls._controls.get(action.strip().lower())
        if method is None:
            return None
        return next(name for name, _ in cls._control_aliases.items()
                    if cls._controls[name] == method)

    @classmethod
    def control_aliases(cls) -> dict[str, list[str]]:
        return {name: list(aliases) for name, aliases in sorted(cls._control_aliases.items())}

    @classmethod
    def physical_controls(cls) -> list[str]:
        """Действия, которые выполняет Оператор руками (агенту недоступны)."""
        return sorted(cls._physical)

    def real_command(self, store: StateStore, action: str, value: Any = None) -> list[tuple[str, Any]]:
        """Перевод команды для узла в режиме real в команды реального драйвера.
        По умолчанию команда уходит на железо как есть. Узлы, у драйверов которых
        нет части действий (щиток: reset_protection), переопределяют перевод и
        при необходимости сами обновляют своё состояние в хранилище."""
        return [(action, value)]

    def handle_control(self, store: StateStore, action: str, value: Any = None) -> list[Event]:
        """Выполняет управляющее воздействие. Бросает ControlError при ошибке.
        Вызывается фасадом Simulator под блокировкой тика."""
        method_name = self._controls.get(action.strip().lower())
        if method_name is None:
            raise ControlError(
                f"Узел {self.node_id!r} ({self.code_name}) не поддерживает действие {action!r}. "
                f"Доступно: {self.available_controls()}"
            )
        node = store.get_node(self.node_id)
        updates = getattr(self, method_name)(node, value)
        if updates:
            store.update(self.node_id, **updates)
        return self._drain_events()

    def _drain_events(self) -> list[Event]:
        events, self._events = self._events, []
        return events


class SensorNode(SandboxNode):
    """
    Датчик: общие поля sensor_state (OK / DEGRADED / FAULT) и confidence (0..1).

    - Редкие самопроизвольные сбои (glitch_rate_per_hour): датчик на 5–40 игровых
      минут становится DEGRADED — шум возрастает, достоверность падает.
    - control_fault: вход для кризисов. Любое непустое значение переводит
      датчик в FAULT (показания замирают, достоверность ~0; в телеметрии — null, Д4).
    - control_noise_multiplier: вход для кризисов (Д5) — множитель шума показаний
      (наводки от ШИМ освещения и т.п.).
    """

    base_confidence: float = 0.97

    def __init__(self, node_id: str, glitch_rate_per_hour: float = 0.01, seed: Optional[int] = None) -> None:
        super().__init__(node_id, seed)
        self.glitch_rate_per_hour = glitch_rate_per_hour
        self._glitch_remaining_s = 0.0

    def get_state(self) -> dict[str, Any]:
        state = super().get_state()
        state.update(sensor_state="OK", confidence=self.base_confidence, control_fault=None,
                     control_noise_multiplier=1.0)
        return state

    def sensor_condition(self, gdt: float, node: dict[str, Any]) -> tuple[str, float, float]:
        """Возвращает (sensor_state, confidence, множитель шума) на этот шаг."""
        if node.get("control_fault"):
            return "FAULT", 0.05, 0.0
        if self._glitch_remaining_s > 0:
            self._glitch_remaining_s -= gdt
        elif self.happens(self.glitch_rate_per_hour, gdt):
            self._glitch_remaining_s = self.rng.uniform(300.0, 2400.0)
        if self._glitch_remaining_s > 0:
            return "DEGRADED", round(self.rng.uniform(0.3, 0.6), 2), 5.0
        return "OK", round(clamp(self.base_confidence + self.noise(0.01), 0.0, 1.0), 3), 1.0

    def simulate(self, gdt: float, node: dict[str, Any]) -> Optional[dict[str, Any]]:
        sensor_state, confidence, noise_k = self.sensor_condition(gdt, node)
        updates: dict[str, Any] = {"sensor_state": sensor_state, "confidence": confidence}
        if node["sensor_state"] != sensor_state and sensor_state != "OK":
            self.emit("sensor.degraded", Severity.WARNING, sensor_state=sensor_state)
        if sensor_state != "FAULT":
            noise_k *= float(node.get("control_noise_multiplier") or 1.0)
            updates.update(self.measure(gdt, node, noise_k) or {})
        return updates

    @abstractmethod
    def measure(self, gdt: float, node: dict[str, Any], noise_k: float) -> Optional[dict[str, Any]]:
        """Показания датчика за шаг. noise_k — множитель шума (растёт при сбое)."""
        raise NotImplementedError


def parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes", "да"}
    return bool(value)


def parse_number(value: Any, lo: float, hi: float, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ControlError(f"{what}: ожидалось число, получено {value!r}") from None
    if not lo <= number <= hi:
        raise ControlError(f"{what}: значение {number} вне диапазона [{lo}, {hi}]")
    return number


def parse_choice(value: Any, choices: list[str] | tuple[str, ...], what: str) -> str:
    if not isinstance(value, str) or value.strip().upper() not in choices:
        raise ControlError(f"{what}: ожидалось одно из {list(choices)}, получено {value!r}")
    return value.strip().upper()

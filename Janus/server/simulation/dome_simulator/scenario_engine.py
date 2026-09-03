# dome_simulator/scenario_engine.py
"""
Движок сценариев и кризисов (п.4.7 ТЗ).

CrisisScenario — шаблон эффекта с жизненным циклом on_start/on_tick/on_end
и условием завершения. ScenarioEngine управляет запуском (ручным, по
условию состояния, по таймеру, случайным) и снятием эффектов.
"""
from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from .events import Event, EventBus, Severity
from .store import StateStore

logger = logging.getLogger("dome_simulator.scenario_engine")


class ScenarioStatus(str, Enum):
    IDLE = "idle"
    ACTIVE = "active"
    FINISHED = "finished"


class CrisisScenario:
    """
    Базовый класс кризисного сценария. Наследники переопределяют
    on_start/on_tick/on_end и, при необходимости, is_finished.

    duration_s: если задан — сценарий завершается сам по истечении времени
    (используется большинством стартовых кризисов). Если None — завершение
    только через is_finished() или явный stop_crisis().
    """

    name: str = "generic_crisis"
    duration_s: Optional[float] = 60.0

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        self.params = params or {}
        self.elapsed_s: float = 0.0
        self.status: ScenarioStatus = ScenarioStatus.IDLE

    # ---- переопределяемые хуки ----

    def on_start(self, store: StateStore, event_bus: EventBus) -> None:
        """Однократный эффект при запуске (например, публикация crisis.* события)."""

    def on_tick(self, store: StateStore, event_bus: EventBus, dt: float) -> None:
        """Эффект, повторяющийся каждый тик, пока сценарий активен."""

    def on_end(self, store: StateStore, event_bus: EventBus) -> None:
        """Снятие эффекта при завершении (естественном или принудительном)."""

    def is_finished(self, store: StateStore) -> bool:
        """Дополнительное условие завершения помимо duration_s.
        По умолчанию — всегда False (завершение только по таймеру/stop)."""
        return False

    # ---- внутренняя механика ----

    def _tick(self, store: StateStore, event_bus: EventBus, dt: float) -> bool:
        """Возвращает True, если сценарий продолжает работать."""
        self.elapsed_s += dt
        self.on_tick(store, event_bus, dt)

        if self.duration_s is not None and self.elapsed_s >= self.duration_s:
            return False
        if self.is_finished(store):
            return False
        return True


ScenarioFactory = Callable[[Optional[dict[str, Any]]], CrisisScenario]


@dataclass(slots=True)
class TimerTrigger:
    scenario_name: str
    interval_s: float
    params: dict[str, Any] = field(default_factory=dict)
    _accumulated_s: float = 0.0


@dataclass(slots=True)
class RandomTrigger:
    scenario_name: str
    probability_per_tick: float  # 0..1, шанс запуска в каждом тике (при dt>0)
    params: dict[str, Any] = field(default_factory=dict)
    cooldown_s: float = 0.0      # минимальный интервал между случайными срабатываниями
    _cooldown_remaining_s: float = 0.0


@dataclass(slots=True)
class ConditionTrigger:
    scenario_name: str
    predicate: Callable[[StateStore], bool]
    params: dict[str, Any] = field(default_factory=dict)
    cooldown_s: float = 30.0     # защита от повторного мгновенного ретриггера
    _cooldown_remaining_s: float = 0.0


class ScenarioEngine:
    def __init__(self) -> None:
        self._templates: dict[str, ScenarioFactory] = {}
        self._active: dict[str, CrisisScenario] = {}
        self._timers: list[TimerTrigger] = []
        self._randoms: list[RandomTrigger] = []
        self._conditions: list[ConditionTrigger] = []
        self._lock = threading.RLock()

    # ---------- регистрация шаблонов ----------

    def register_scenario_type(self, name: str, factory: ScenarioFactory) -> None:
        with self._lock:
            self._templates[name] = factory

    def add_timer_trigger(self, trigger: TimerTrigger) -> None:
        with self._lock:
            self._timers.append(trigger)

    def add_random_trigger(self, trigger: RandomTrigger) -> None:
        with self._lock:
            self._randoms.append(trigger)

    def add_condition_trigger(self, trigger: ConditionTrigger) -> None:
        with self._lock:
            self._conditions.append(trigger)

    # ---------- публичный API (п.4.1: trigger_crisis / stop_crisis) ----------

    def trigger_crisis(self, name: str, store: StateStore, event_bus: EventBus,
                        params: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            if name in self._active and self._active[name].status == ScenarioStatus.ACTIVE:
                logger.warning("Кризис %r уже активен, повторный запуск игнорируется", name)
                return
            factory = self._templates.get(name)
            if factory is None:
                raise KeyError(f"Неизвестный сценарий {name!r}. Доступно: {sorted(self._templates)}")
            scenario = factory(params)
            scenario.status = ScenarioStatus.ACTIVE
            self._active[name] = scenario

        scenario.on_start(store, event_bus)
        logger.info("Кризис %r запущен с параметрами %s", name, params)

    def stop_crisis(self, name: str, store: StateStore, event_bus: EventBus) -> None:
        with self._lock:
            scenario = self._active.get(name)
            if scenario is None or scenario.status != ScenarioStatus.ACTIVE:
                logger.warning("Кризис %r не активен, stop_crisis игнорируется", name)
                return
            scenario.status = ScenarioStatus.FINISHED

        scenario.on_end(store, event_bus)
        logger.info("Кризис %r остановлен вручную", name)

    def active_crises(self) -> list[str]:
        with self._lock:
            return [n for n, s in self._active.items() if s.status == ScenarioStatus.ACTIVE]

    # ---------- главный тик (вызывается SimulatorThread) ----------

    def tick(self, store: StateStore, event_bus: EventBus, dt: float) -> None:
        self._advance_active(store, event_bus, dt)
        if dt <= 0:
            return  # на паузе новые кризисы не планируем
        self._check_timers(store, event_bus, dt)
        self._check_randoms(store, event_bus, dt)
        self._check_conditions(store, event_bus, dt)

    def _advance_active(self, store: StateStore, event_bus: EventBus, dt: float) -> None:
        with self._lock:
            names = list(self._active.keys())
        for name in names:
            scenario = self._active.get(name)
            if scenario is None or scenario.status != ScenarioStatus.ACTIVE:
                continue
            still_running = scenario._tick(store, event_bus, dt)
            if not still_running:
                scenario.status = ScenarioStatus.FINISHED
                scenario.on_end(store, event_bus)
                logger.info("Кризис %r завершён естественным образом", name)

    def _check_timers(self, store: StateStore, event_bus: EventBus, dt: float) -> None:
        for trig in self._timers:
            trig._accumulated_s += dt
            if trig._accumulated_s >= trig.interval_s:
                trig._accumulated_s = 0.0
                self._safe_trigger(trig.scenario_name, store, event_bus, trig.params)

    def _check_randoms(self, store: StateStore, event_bus: EventBus, dt: float) -> None:
        for trig in self._randoms:
            if trig._cooldown_remaining_s > 0:
                trig._cooldown_remaining_s = max(0.0, trig._cooldown_remaining_s - dt)
                continue
            if random.random() < trig.probability_per_tick:
                trig._cooldown_remaining_s = trig.cooldown_s
                self._safe_trigger(trig.scenario_name, store, event_bus, trig.params)

    def _check_conditions(self, store: StateStore, event_bus: EventBus, dt: float) -> None:
        for trig in self._conditions:
            if trig._cooldown_remaining_s > 0:
                trig._cooldown_remaining_s = max(0.0, trig._cooldown_remaining_s - dt)
                continue
            try:
                triggered = trig.predicate(store)
            except Exception:
                logger.exception("Ошибка в предикате condition-триггера для %r", trig.scenario_name)
                continue
            if triggered:
                trig._cooldown_remaining_s = trig.cooldown_s
                self._safe_trigger(trig.scenario_name, store, event_bus, trig.params)

    def _safe_trigger(self, name: str, store: StateStore, event_bus: EventBus,
                       params: dict[str, Any]) -> None:
        try:
            self.trigger_crisis(name, store, event_bus, params)
        except Exception:
            logger.exception("Не удалось автоматически запустить сценарий %r", name)
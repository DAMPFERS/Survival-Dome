# dome_simulator/scenario_engine.py
"""
Движок сценариев и кризисов (п.4.7 ТЗ, доработка Д17 из dome_crises.md).

CrisisScenario — кризис с жизненным циклом:
    ожидание предусловия (WAITING) → on_start → on_tick… → on_end.
Кризис развивается фазами (предвестник → развитие → последствие); переход к
последствию отменяется, если агент выполнил критерий решения (окно реакции).

Каждый кризис ведёт отчёт (CrisisReport): статус RESOLVED / FAILED / EXPIRED,
время первой релевантной реакции агента, время решения, ошибочные действия.

Кризис может перехватывать команды (Д7): intercept() вызывается до выполнения
команды узлом — например, reset_alarm фрезера при подменённой аварии снимает
подмену и выполняет безопасный реальный эквивалент (resume).

Запуск — вручную (trigger_crisis), по таймеру, по условию, случайно, а также из
сценария смены (shift.py). Имя кризиса можно задавать псевдонимом: "6", "№6",
"n6", "К1", "K1", "k1".
"""
from __future__ import annotations

import logging
import random
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from .context import Command, SimContext
from .events import Severity

logger = logging.getLogger("dome_simulator.scenario_engine")


class ScenarioStatus(str, Enum):
    IDLE = "idle"
    WAITING = "waiting"
    ACTIVE = "active"
    FINISHED = "finished"


@dataclass
class CrisisReport:
    name: str
    code: str = ""
    title: str = ""
    klass: str = ""
    kind: str = ""
    triggered_s: Optional[float] = None
    started_s: Optional[float] = None
    ended_s: Optional[float] = None
    outcome: str = "PENDING"            # PENDING | RESOLVED | FAILED | EXPIRED | MANUAL | NOT_STARTED
    first_action_s: Optional[float] = None
    resolved_s: Optional[float] = None
    phase: str = ""
    notes: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    operator_actions: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    def reaction_time_s(self) -> Optional[float]:
        if self.first_action_s is None or self.started_s is None:
            return None
        return round(self.first_action_s - self.started_s, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "code": self.code, "title": self.title, "class": self.klass, "type": self.kind,
            "triggered_s": _r(self.triggered_s), "started_s": _r(self.started_s), "ended_s": _r(self.ended_s),
            "outcome": self.outcome, "phase": self.phase, "reaction_time_s": self.reaction_time_s(),
            "resolved_s": _r(self.resolved_s), "notes": list(self.notes), "penalties": list(self.penalties),
            "operator_actions": list(self.operator_actions),
            "params": {k: v for k, v in self.params.items() if isinstance(v, (int, float, str, bool, type(None)))},
        }


def _r(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 1)


@dataclass
class Intercept:
    """Результат перехвата команды кризисом.
    handled=True — команда узлу не передаётся (кризис выполнил её сам);
    error — команда отклоняется с этой ошибкой;
    real_actions — что отправить реальному устройству вместо исходной команды."""
    handled: bool = True
    error: Optional[str] = None
    real_actions: Optional[list[tuple[str, Any]]] = None


class CrisisScenario:
    """
    Базовый класс кризиса. Наследники переопределяют хуки; удобные помощники
    (подмены, заморозки, входы узлов, окружение) — в crises.Crisis.

    duration_s: если задан — кризис завершается сам по истечении времени
    (отсчёт с момента старта, не с ожидания предусловия). None — только через
    is_finished() или stop_crisis().
    wait_timeout_s: сколько ждать предусловия (None — бесконечно).
    """

    name: str = "generic_crisis"
    code: str = ""
    title: str = ""
    klass: str = ""                  # П0 | П1 | П2 | П3
    kind: str = ""                   # A | B | C | D
    duration_s: Optional[float] = 60.0
    wait_timeout_s: Optional[float] = 600.0

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        self.params = dict(params or {})
        if "duration_s" in self.params:
            self.duration_s = None if self.params["duration_s"] is None else float(self.params["duration_s"])
        self.elapsed_s: float = 0.0
        self.waited_s: float = 0.0
        self.status: ScenarioStatus = ScenarioStatus.IDLE
        self.report = CrisisReport(name=self.name, code=self.code, title=self.title,
                                   klass=self.klass, kind=self.kind, params=dict(self.params))

    # ---- переопределяемые хуки ----

    def precondition(self, ctx: SimContext) -> bool:
        """Можно ли начинать (например, фрезер должен работать). По умолчанию — сразу."""
        return True

    def on_start(self, ctx: SimContext) -> None:
        """Однократный эффект при запуске."""

    def on_tick(self, ctx: SimContext, dt: float) -> None:
        """Эффект, повторяющийся каждый тик, пока кризис активен."""

    def on_end(self, ctx: SimContext) -> None:
        """Снятие эффекта при завершении (естественном или принудительном)."""

    def is_finished(self, ctx: SimContext) -> bool:
        return False

    def intercept(self, ctx: SimContext, cmd: Command) -> Optional[Intercept]:
        """Перехват команды до её выполнения (Д7). None — не перехватывать."""
        return None

    def on_command(self, ctx: SimContext, cmd: Command) -> None:
        """Наблюдение за выполненной командой (реакция агента, ошибочные действия)."""

    # ---- внутренняя механика ----

    def _begin(self, ctx: SimContext) -> None:
        self.report.triggered_s = ctx.now_s
        if self.precondition(ctx):
            self._start(ctx)
        else:
            self.status = ScenarioStatus.WAITING
            self.report.phase = "WAITING"

    def _start(self, ctx: SimContext) -> None:
        self.status = ScenarioStatus.ACTIVE
        self.report.started_s = ctx.now_s
        self.report.outcome = "PENDING"
        self.on_start(ctx)
        ctx.emit("crisis.started", Severity.WARNING, source=f"crisis:{self.name}",
                 crisis=self.name, code=self.code, title=self.title)

    def _tick(self, ctx: SimContext, dt: float) -> bool:
        """Возвращает True, если кризис продолжает работать."""
        if self.status == ScenarioStatus.WAITING:
            self.waited_s += dt
            if self.precondition(ctx):
                self._start(ctx)
            elif self.wait_timeout_s is not None and self.waited_s >= self.wait_timeout_s:
                self.report.outcome = "NOT_STARTED"
                self.report.notes.append("Предусловие не выполнилось за отведённое время")
                return False
            return True
        self.elapsed_s += dt
        self.on_tick(ctx, dt)
        if self.duration_s is not None and self.elapsed_s >= self.duration_s:
            return False
        if self.is_finished(ctx):
            return False
        return True

    def _finish(self, ctx: SimContext) -> None:
        was_active = self.report.started_s is not None
        self.status = ScenarioStatus.FINISHED
        self.report.ended_s = ctx.now_s
        if was_active:
            self.on_end(ctx)
            if self.report.outcome == "PENDING":
                self.report.outcome = "EXPIRED"
        ctx.emit("crisis.finished", Severity.INFO, source=f"crisis:{self.name}",
                 crisis=self.name, outcome=self.report.outcome)


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
    predicate: Callable[[SimContext], bool]
    params: dict[str, Any] = field(default_factory=dict)
    cooldown_s: float = 30.0     # защита от повторного мгновенного ретриггера
    _cooldown_remaining_s: float = 0.0


_ALIAS_RE = re.compile(r"^(?:№|n|#)?\s*(\d{1,2})$", re.IGNORECASE)
_K_ALIAS_RE = re.compile(r"^[кk]\s*(\d{1,2})$", re.IGNORECASE)


class ScenarioEngine:
    def __init__(self) -> None:
        self._templates: dict[str, ScenarioFactory] = {}
        self._aliases: dict[str, str] = {}
        self._active: dict[str, CrisisScenario] = {}
        self._history: list[CrisisScenario] = []
        self._timers: list[TimerTrigger] = []
        self._randoms: list[RandomTrigger] = []
        self._conditions: list[ConditionTrigger] = []
        self._lock = threading.RLock()
        self.rng = random.Random()

    # ---------- регистрация шаблонов ----------

    def register_scenario_type(self, name: str, factory: ScenarioFactory, aliases: tuple[str, ...] = ()) -> None:
        with self._lock:
            self._templates[name] = factory
            for alias in aliases:
                self._aliases[alias.lower()] = name

    def resolve_name(self, name: str) -> str:
        key = name.strip()
        if key in self._templates:
            return key
        low = key.lower()
        if low in self._aliases:
            return self._aliases[low]
        m = _ALIAS_RE.match(low)
        if m:
            prefix = f"n{int(m.group(1)):02d}_"
            found = [n for n in self._templates if n.startswith(prefix)]
            if found:
                return found[0]
        m = _K_ALIAS_RE.match(low)
        if m:
            prefix = f"k{int(m.group(1)):02d}_"
            found = [n for n in self._templates if n.startswith(prefix)]
            if found:
                return found[0]
        raise KeyError(f"Неизвестный сценарий {name!r}. Доступно: {sorted(self._templates)}")

    def available(self) -> list[dict[str, Any]]:
        catalog = []
        with self._lock:
            for name, factory in sorted(self._templates.items()):
                try:
                    proto = factory(None)
                except Exception:  # фабрика требует параметров
                    catalog.append({"name": name})
                    continue
                catalog.append({"name": name, "code": proto.code, "title": proto.title,
                                "class": proto.klass, "type": proto.kind, "duration_s": proto.duration_s})
        return catalog

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

    def trigger_crisis(self, name: str, ctx: SimContext, params: Optional[dict[str, Any]] = None) -> Optional[CrisisScenario]:
        with self._lock:
            name = self.resolve_name(name)
            current = self._active.get(name)
            if current is not None and current.status in (ScenarioStatus.ACTIVE, ScenarioStatus.WAITING):
                logger.warning("Кризис %r уже активен, повторный запуск игнорируется", name)
                return None
            scenario = self._templates[name](params)
            scenario.name = name
            scenario.report.name = name
            scenario.status = ScenarioStatus.ACTIVE
            self._active[name] = scenario
        scenario._begin(ctx)
        logger.info("Кризис %r запущен с параметрами %s", name, params)
        return scenario

    def stop_crisis(self, name: str, ctx: SimContext) -> None:
        with self._lock:
            try:
                name = self.resolve_name(name)
            except KeyError:
                pass
            scenario = self._active.get(name)
            if scenario is None or scenario.status not in (ScenarioStatus.ACTIVE, ScenarioStatus.WAITING):
                logger.warning("Кризис %r не активен, stop_crisis игнорируется", name)
                return
        self._finish(name, scenario, ctx)
        logger.info("Кризис %r остановлен вручную", name)

    def stop_all(self, ctx: SimContext) -> None:
        for name in self.active_crises(include_waiting=True):
            self.stop_crisis(name, ctx)

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def active_crises(self, include_waiting: bool = False) -> list[str]:
        states = (ScenarioStatus.ACTIVE, ScenarioStatus.WAITING) if include_waiting else (ScenarioStatus.ACTIVE,)
        with self._lock:
            return [n for n, s in self._active.items() if s.status in states]

    def get(self, name: str) -> Optional[CrisisScenario]:
        with self._lock:
            try:
                name = self.resolve_name(name)
            except KeyError:
                return None
            if name in self._active:
                return self._active[name]
            for s in reversed(self._history):
                if s.name == name:
                    return s
            return None

    def reports(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history) + [s for s in self._active.values() if s.status != ScenarioStatus.FINISHED]
        return [s.report.to_dict() for s in items]

    def _finish(self, name: str, scenario: CrisisScenario, ctx: SimContext) -> None:
        try:
            scenario._finish(ctx)
        finally:
            with self._lock:
                if self._active.get(name) is scenario:
                    del self._active[name]
                self._history.append(scenario)

    # ---------- команды (Д7) ----------

    def _running(self) -> list[CrisisScenario]:
        with self._lock:
            return [s for s in self._active.values() if s.status == ScenarioStatus.ACTIVE]

    def intercept(self, ctx: SimContext, cmd: Command) -> Optional[Intercept]:
        for scenario in self._running():
            try:
                result = scenario.intercept(ctx, cmd)
            except Exception:
                logger.exception("Ошибка перехвата команды в кризисе %r", scenario.name)
                continue
            if result is not None:
                cmd.intercepted_by = scenario.name
                return result
        return None

    def notify(self, ctx: SimContext, cmd: Command) -> None:
        for scenario in self._running():
            try:
                scenario.on_command(ctx, cmd)
            except Exception:
                logger.exception("Ошибка обработки команды в кризисе %r", scenario.name)

    # ---------- главный тик (вызывается SimulatorThread) ----------

    def tick(self, ctx: SimContext, dt: float) -> None:
        self._advance_active(ctx, dt)
        if dt <= 0:
            return  # на паузе новые кризисы не планируем
        self._check_timers(ctx, dt)
        self._check_randoms(ctx, dt)
        self._check_conditions(ctx, dt)

    def _advance_active(self, ctx: SimContext, dt: float) -> None:
        if dt <= 0:
            return
        with self._lock:
            items = list(self._active.items())
        for name, scenario in items:
            if scenario.status not in (ScenarioStatus.ACTIVE, ScenarioStatus.WAITING):
                continue
            try:
                still_running = scenario._tick(ctx, dt)
            except Exception:
                logger.exception("Ошибка в тике кризиса %r — кризис остановлен", name)
                still_running = False
            if not still_running:
                self._finish(name, scenario, ctx)
                logger.info("Кризис %r завершён: %s", name, scenario.report.outcome)

    def _check_timers(self, ctx: SimContext, dt: float) -> None:
        for trig in self._timers:
            trig._accumulated_s += dt
            if trig._accumulated_s >= trig.interval_s:
                trig._accumulated_s = 0.0
                self._safe_trigger(trig.scenario_name, ctx, trig.params)

    def _check_randoms(self, ctx: SimContext, dt: float) -> None:
        for trig in self._randoms:
            if trig._cooldown_remaining_s > 0:
                trig._cooldown_remaining_s = max(0.0, trig._cooldown_remaining_s - dt)
                continue
            if self.rng.random() < trig.probability_per_tick:
                trig._cooldown_remaining_s = trig.cooldown_s
                self._safe_trigger(trig.scenario_name, ctx, trig.params)

    def _check_conditions(self, ctx: SimContext, dt: float) -> None:
        for trig in self._conditions:
            if trig._cooldown_remaining_s > 0:
                trig._cooldown_remaining_s = max(0.0, trig._cooldown_remaining_s - dt)
                continue
            try:
                triggered = trig.predicate(ctx)
            except Exception:
                logger.exception("Ошибка в предикате condition-триггера для %r", trig.scenario_name)
                continue
            if triggered:
                trig._cooldown_remaining_s = trig.cooldown_s
                self._safe_trigger(trig.scenario_name, ctx, trig.params)

    def _safe_trigger(self, name: str, ctx: SimContext, params: dict[str, Any]) -> None:
        try:
            self.trigger_crisis(name, ctx, params)
        except Exception:
            logger.exception("Не удалось автоматически запустить сценарий %r", name)

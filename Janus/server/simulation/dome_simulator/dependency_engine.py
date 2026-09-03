# dome_simulator/dependency_engine.py
"""
Движок правил зависимостей между узлами.

Правило = Callable[[StateStore, EventBus, float], None], где float — dt
текущего тика (обоснование отступления от буквального ТЗ — см. пояснение
в сопроводительном тексте). Движок не знает о конкретных типах узлов —
правила сами распознают роли узлов по набору полей в их состоянии.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

from .events import Event, EventBus, Severity
from .store import StateStore

logger = logging.getLogger("dome_simulator.dependency_engine")

RuleFunc = Callable[[StateStore, EventBus, float], None]


@dataclass(slots=True)
class Rule:
    name: str
    func: RuleFunc
    enabled: bool = True


class DependencyEngine:
    """Хранит упорядоченный набор правил и выполняет их каждый тик."""

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}
        self._order: list[str] = []  # порядок важен: правила могут зависеть друг от друга по данным
        self._lock = threading.RLock()

    def add_rule(self, name: str, func: RuleFunc, enabled: bool = True) -> None:
        with self._lock:
            if name in self._rules:
                raise ValueError(f"Правило {name!r} уже зарегистрировано")
            self._rules[name] = Rule(name=name, func=func, enabled=enabled)
            self._order.append(name)

    def remove_rule(self, name: str) -> None:
        with self._lock:
            self._rules.pop(name, None)
            if name in self._order:
                self._order.remove(name)

    def enable(self, name: str) -> None:
        with self._lock:
            self._rules[name].enabled = True

    def disable(self, name: str) -> None:
        with self._lock:
            self._rules[name].enabled = False

    def list_rules(self) -> list[str]:
        with self._lock:
            return [f"{n} ({'on' if self._rules[n].enabled else 'off'})" for n in self._order]

    def run(self, store: StateStore, event_bus: EventBus, dt: float) -> None:
        """Вызывается SimulatorThread один раз за тик, СРАЗУ ПОСЛЕ tick() всех узлов
        и ДО event_bus.dispatch_pending() — так правила успевают среагировать
        на события узлов текущего тика (line.tripped, battery.critical и т.п.)
        ещё до того, как подписчики их увидят."""
        with self._lock:
            names = list(self._order)
        for name in names:
            rule = self._rules.get(name)
            if rule is None or not rule.enabled:
                continue
            try:
                rule.func(store, event_bus, dt)
            except Exception:
                logger.exception("Ошибка в правиле зависимости %r", name)
                event_bus.publish(Event(
                    type="dependency.violation",
                    source=f"dependency_engine:{name}",
                    payload={"error": "rule_execution_failed"},
                    severity=Severity.WARNING,
                ))
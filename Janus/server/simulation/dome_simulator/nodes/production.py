# dome_simulator/nodes/production.py
"""
Производственные узлы. Ключевое отличие от прочих категорий: у них
есть понятие "задания" (job) с длительностью, и apply_control реально
что-то ВЫЧИСЛЯЕТ (валидация job) перед тем как его принять — здесь тот
самый случай "непустого apply_control", о котором говорилось в части 3.
"""
from __future__ import annotations

from typing import Any

from ..events import Event, EventBus, Severity
from ..store import StateStore
from .base import BaseNode, register_node_type


class _JobRunnerMixin:
    """Общая логика "запустить job на N секунд, потребляя энергию"."""

    def _init_job_state(self, power_draw_kw: float) -> dict[str, Any]:
        return {
            "power_draw_kw": power_draw_kw,
            "status": "idle",          # idle | running | interrupted | done
            "job_name": None,
            "job_remaining_s": 0.0,
            "control_powered": True,
        }

    def _tick_job(self, node_id: str, dt: float, state: StateStore) -> list[Event]:
        events: list[Event] = []
        node = state.get_node(node_id)

        if node["status"] != "running":
            return events

        if not node["control_powered"]:
            state.update(node_id, status="interrupted")
            events.append(Event(
                type="production.interrupted", source=node_id,
                payload={"job_name": node["job_name"], "reason": "power_loss"},
                severity=Severity.WARNING,
            ))
            return events

        remaining = max(0.0, node["job_remaining_s"] - dt)
        if remaining == 0.0:
            state.update(node_id, status="done", job_remaining_s=0.0)
            events.append(Event(
                type="production.job_done", source=node_id,
                payload={"job_name": node["job_name"]}, severity=Severity.INFO,
            ))
        else:
            state.update(node_id, job_remaining_s=round(remaining, 2))
        return events

    def _start_job(self, node_id: str, state: StateStore, job_name: str, duration_s: float) -> None:
        """Валидация + запуск — вызывается из apply_control конкретного узла."""
        if duration_s <= 0:
            raise ValueError("duration_s должен быть положительным")
        node = state.get_node(node_id)
        if node["status"] == "running":
            raise RuntimeError(f"Узел {node_id} уже выполняет задание {node['job_name']!r}")
        state.update(node_id, status="running", job_name=job_name, job_remaining_s=duration_s)


@register_node_type("cnc_mill")
class CNC_Mill(BaseNode, _JobRunnerMixin):
    category = "production"

    def __init__(self, node_id: str, power_draw_kw: float = 2.5) -> None:
        BaseNode.__init__(self, node_id)
        self.power_draw_kw = power_draw_kw

    def get_state(self) -> dict[str, Any]:
        return self._init_job_state(self.power_draw_kw)

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        return self._tick_job(self.node_id, dt, state)

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        # value для start_job ожидается как dict: {"job_name": str, "duration_s": float}
        if action == "start_job":
            raise NotImplementedError("Вызывается фасадом с доступом к StateStore, см. simulator.py")
        elif action not in {"cancel_job"}:
            super().apply_control(action, value)


@register_node_type("printer_3d")
class Printer3D(BaseNode, _JobRunnerMixin):
    category = "production"

    def __init__(self, node_id: str, power_draw_kw: float = 0.6) -> None:
        BaseNode.__init__(self, node_id)
        self.power_draw_kw = power_draw_kw

    def get_state(self) -> dict[str, Any]:
        return self._init_job_state(self.power_draw_kw)

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        return self._tick_job(self.node_id, dt, state)

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"start_job", "cancel_job"}:
            super().apply_control(action, value)


@register_node_type("soldering_station")
class SolderingStation(BaseNode, _JobRunnerMixin):
    category = "production"

    def __init__(self, node_id: str, power_draw_kw: float = 0.3) -> None:
        BaseNode.__init__(self, node_id)
        self.power_draw_kw = power_draw_kw

    def get_state(self) -> dict[str, Any]:
        return self._init_job_state(self.power_draw_kw)

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        return self._tick_job(self.node_id, dt, state)

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"start_job", "cancel_job"}:
            super().apply_control(action, value)


@register_node_type("work_station")
class WorkStation(BaseNode, _JobRunnerMixin):
    """Универсальное рабочее место (сборка/тестирование), несколько на купол."""
    category = "production"

    def __init__(self, node_id: str, power_draw_kw: float = 0.4) -> None:
        BaseNode.__init__(self, node_id)
        self.power_draw_kw = power_draw_kw

    def get_state(self) -> dict[str, Any]:
        return self._init_job_state(self.power_draw_kw)

    def tick(self, dt: float, state: StateStore, event_bus: EventBus) -> list[Event]:
        return self._tick_job(self.node_id, dt, state)

    def on_event(self, event: Event) -> None:
        pass

    def apply_control(self, action: str, value: Any = None) -> None:
        if action not in {"start_job", "cancel_job"}:
            super().apply_control(action, value)
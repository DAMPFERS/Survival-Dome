# dome_simulator/crisis_base.py
"""
Crisis — база для кризисов из dome_crises.md: помощники для четырёх типов
воздействия и для оценки агента.

    A. подмена показаний   — distort(...)            (телеметрия, telemetry.py)
    B. воздействие          — cmd(...), set_input(...) (команды и входы control_* узлов)
    C. событие окружения    — force_env(...), offset_env(...), env.drivers
    D. инфраструктура       — freeze(...)             (заморозка узла / связи)

Всё, что кризис изменил через помощники, снимается автоматически в on_end
(подмены и заморозки — сразу, входы узлов — восстанавливаются, если
restore=True). Физические последствия (сработавшее УЗО, испорченная деталь)
остаются — их устраняет агент/Оператор.

Оценка (dome_crises.md, раздел 9): first_action_s — первая релевантная команда
агента (relevant_actions), resolve()/fail() — критерий решения, penalty() —
ошибочные действия. finalize() вызывается в конце: по умолчанию кризис без
провала считается RESOLVED, если success_if_not_failed=True.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

from .context import Command, SimContext
from .events import Severity
from .scenario_engine import CrisisScenario, Intercept  # noqa: F401  (реэкспорт для кризисов)

PRODUCTION_LINES = (1, 2, 3, 6)
PANEL, PRINTER, CNC, INVERTER, BATTERY = ("smart_panel_01", "printer_3d_01", "cnc_01",
                                          "solar_inverter_01", "battery_01")
AUTO = "dome_automation_01"


class Crisis(CrisisScenario):
    relevant_actions: tuple = ()        # (node_id, action) — "*" = любое; или (node_id, action, value)
    success_if_not_failed: bool = True  # в конце без провала — RESOLVED

    def __init__(self, params: Optional[dict[str, Any]] = None) -> None:
        super().__init__(params)
        self._restore: list[tuple[str, str, Any]] = []
        self._forced: list[str] = []
        self._offsets: list[str] = []
        self._timers: dict[str, float] = {}
        self.failed = False
        self.resolved = False

    # ---------------- параметры ----------------

    def p(self, key: str, default: Any = None) -> Any:
        value = self.params.get(key, default)
        return default if value is None else value

    # ---------------- воздействия ----------------

    def distort(self, ctx: SimContext, node_id: str, path: str, mode: str, **params: Any) -> Optional[str]:
        if not ctx.has(node_id):
            return None
        return ctx.telemetry.add_distortion(node_id, path, mode, owner=self.name, **params)

    def undistort(self, ctx: SimContext, node_id: Optional[str] = None) -> None:
        """Снять подмены кризиса (все или только у узла)."""
        for d in ctx.telemetry.distortions_of(self.name):
            if node_id is None or d["node_id"] == node_id:
                ctx.telemetry.remove_distortion(d["id"])

    def freeze(self, ctx: SimContext, node_id: str, **kwargs: Any) -> None:
        ctx.telemetry.freeze_node(node_id, self.name, **kwargs)

    def unfreeze(self, ctx: SimContext, node_id: str) -> None:
        ctx.telemetry.unfreeze_node(node_id, self.name)

    def set_input(self, ctx: SimContext, node_id: str, key: str, value: Any, restore: bool = True) -> None:
        """Выставить поле узла (обычно вход control_*); restore — вернуть исходное в конце."""
        if not ctx.has(node_id):
            return
        if restore and not any(n == node_id and k == key for n, k, _ in self._restore):
            self._restore.append((node_id, key, ctx.get(node_id, key)))
        ctx.update(node_id, **{key: value})

    def cmd(self, ctx: SimContext, node_id: str, action: str, value: Any = None) -> bool:
        return ctx.command(node_id, action, value, origin="crisis")

    def operator_cmd(self, ctx: SimContext, node_id: str, action: str, value: Any = None) -> bool:
        """Действие Оператора по сценарию (виртуальная смена без живого Оператора)."""
        return ctx.command(node_id, action, value, origin="operator")

    def force_env(self, ctx: SimContext, key: str, value: Any) -> None:
        ctx.env.force(key, value)
        if key not in self._forced:
            self._forced.append(key)

    def unforce_env(self, ctx: SimContext, key: str) -> None:
        ctx.env.unforce(key)
        if key in self._forced:
            self._forced.remove(key)

    def offset_env(self, ctx: SimContext, key: str, delta: float) -> None:
        ctx.env.offset(key, delta)
        if key not in self._offsets:
            self._offsets.append(key)

    # ---------------- оценка ----------------

    @property
    def t(self) -> float:
        return self.elapsed_s

    def note(self, text: str) -> None:
        if text not in self.report.notes:
            self.report.notes.append(text)

    def penalty(self, text: str) -> None:
        self.report.penalties.append(text)

    def resolve(self, ctx: SimContext, note: Optional[str] = None) -> None:
        if self.failed or self.resolved:
            return
        self.resolved = True
        self.report.outcome = "RESOLVED"
        self.report.resolved_s = ctx.now_s
        if note:
            self.note(note)
        ctx.emit("crisis.resolved", Severity.INFO, source=f"crisis:{self.name}", crisis=self.name)

    def fail(self, ctx: SimContext, note: Optional[str] = None, force: bool = False) -> None:
        """Провал критерия. Уже решённый кризис не проваливается (если не force)."""
        if self.failed or (self.resolved and not force):
            return
        self.failed = True
        self.resolved = False
        self.report.outcome = "FAILED"
        if note:
            self.note(note)
        ctx.emit("crisis.failed", Severity.WARNING, source=f"crisis:{self.name}", crisis=self.name, note=note)

    def manual(self, note: str) -> None:
        """Критерий не измеряется автоматически (сообщение Оператору и т.п.)."""
        if self.report.outcome in ("PENDING", "EXPIRED"):
            self.report.outcome = "MANUAL"
        self.note(note)

    def timer(self, key: str, active: bool, dt: float) -> float:
        """Непрерывная длительность условия (сбрасывается, когда условие ложно)."""
        self._timers[key] = self._timers.get(key, 0.0) + dt if active else 0.0
        return self._timers[key]

    # ---------------- наблюдение ----------------

    def is_relevant(self, cmd: Command) -> bool:
        for spec in self.relevant_actions:
            node_id, action = spec[0], spec[1]
            if node_id not in ("*", cmd.node_id) or action not in ("*", cmd.action):
                continue
            if len(spec) > 2 and spec[2] is not None:
                if not _value_matches(cmd.value, spec[2]):
                    continue
            return True
        return False

    def on_command(self, ctx: SimContext, cmd: Command) -> None:
        if cmd.origin == "agent" and self.report.first_action_s is None and self.is_relevant(cmd):
            self.report.first_action_s = cmd.t_s
        if cmd.origin == "operator":
            self.report.operator_actions.append(f"{cmd.node_id}.{cmd.action}")
        self.observe(ctx, cmd)

    def observe(self, ctx: SimContext, cmd: Command) -> None:
        """Реакция кризиса на команду (переопределяется)."""

    def on_end(self, ctx: SimContext) -> None:
        try:
            self.cleanup(ctx)
        finally:
            ctx.telemetry.remove_owner(self.name)
            for node_id, key, value in reversed(self._restore):
                if ctx.has(node_id):
                    ctx.update(node_id, **{key: value})
            for key in self._forced:
                ctx.env.unforce(key)
            for key in self._offsets:
                ctx.env.clear_offset(key)
            self.finalize(ctx)

    def cleanup(self, ctx: SimContext) -> None:
        """Дополнительное снятие эффектов (переопределяется)."""

    def finalize(self, ctx: SimContext) -> None:
        if self.report.outcome == "PENDING" and not self.failed and self.success_if_not_failed:
            self.report.outcome = "RESOLVED"
            if self.report.resolved_s is None:
                self.report.resolved_s = ctx.now_s

    # ---------------- типовые проверки ----------------

    @staticmethod
    def interlock(ctx: SimContext, il: str) -> dict[str, Any]:
        return (ctx.get(AUTO, "interlocks", {}) or {}).get(il, {})

    @staticmethod
    def trusted(ctx: SimContext, node_id: str, *paths: str) -> bool:
        """True — ни один из путей не помечен агентом как UNRELIABLE/FAILED."""
        return all(ctx.trust(node_id, p) == "TRUSTED" for p in paths)

    def agent_marked(self, ctx: SimContext, node_id: str, *paths: str) -> bool:
        return any(ctx.trust(node_id, p) != "TRUSTED" for p in paths)

    @staticmethod
    def line_state(ctx: SimContext, n: int) -> str:
        return ctx.line(n)["state"]

    def watch_line_on(self, ctx: SimContext, n: int, dt: float, limit_s: float, what: str) -> None:
        """Провал, если линия n выключена непрерывно дольше limit_s."""
        off = self.line_state(ctx, n) != "ON"
        if self.timer(f"line{n}_off", off, dt) > limit_s:
            self.fail(ctx, f"{what}: линия {n} без питания дольше {limit_s:.0f} с")

    def agent_turned_off(self, cmd: Command, lines: Iterable[int]) -> Optional[int]:
        if cmd.origin == "agent" and cmd.node_id == PANEL and cmd.action == "line_off" and cmd.ok:
            n = _line_of(cmd.value)
            if n in lines:
                return n
        return None


def _line_of(value: Any) -> Optional[int]:
    if isinstance(value, dict):
        value = value.get("line")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _value_matches(value: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(value, dict) and all(str(value.get(k)).upper() == str(v).upper() for k, v in expected.items())
    if isinstance(value, dict):
        return any(str(v).upper() == str(expected).upper() for v in value.values())
    return str(value).upper() == str(expected).upper()


def trust_cmd(node: str, param_part: str = "") -> tuple:
    """Шаблон релевантного действия: пометка источника node (param содержит param_part)."""
    return (AUTO, "set_data_trust", {"node": node})


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

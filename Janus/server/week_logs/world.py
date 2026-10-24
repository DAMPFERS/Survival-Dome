# week_logs/world.py
"""
Купол недельного прогона: симулятор с сутками 24 ч, часы календаря недели, журнал
участника (logger/participant_log.py) и планировщик сценариев экипажа.

Сценарии (распорядок дня, кризисы и реакция на них) — генераторы. Генератор отдаёт
Wait(секунды) или Until(условие, таймаут) и продолжает работу, когда наступил момент;
в ответ на Until получает True (условие выполнено) или False (таймаут).

Команды экипажа идут через фасад симулятора, как команды сервера: agent() — с ключом
команды (origin="agent", права из authorization/access_policy.json), operator() — физические
действия Оператора в куполе. Обе пишутся в журнал участника вместе с ответом. Кризисы
запускает world.trigger() — в журнал это не попадает (разметки в логах нет).
"""
from __future__ import annotations

import logging
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Generator, Optional

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR / "simulation"))
sys.path.insert(0, str(SERVER_DIR))

from authorization.access_policy import ROLE_TEAM, AccessPolicy  # noqa: E402
from dome_simulator import ControlError, create_dome_simulator  # noqa: E402
from dome_simulator.telemetry import get_path  # noqa: E402
from logger.participant_log import ParticipantLogger  # noqa: E402

logger = logging.getLogger("week_logs")

TICK_S = 4.0
DAY_S = 86400.0
POLICY_FILE = SERVER_DIR / "authorization" / "access_policy.json"
TEAM = 1
AGENT_NAME = "Дежурный агент"

# Случайные отказы, которые без человека в контуре портят неделю (пожаротушение по ложной
# тревоге, ошибка печати, которую агенту нечем снять): такие события ставит план недели.
DISABLED_RANDOM_FAULTS = (
    ("smoke_detector_01", "false_alarm_rate_per_hour"),
    ("smoke_detector_02", "false_alarm_rate_per_hour"),
    ("fire_suppression_01", "false_trigger_rate_per_hour"),
    ("smart_panel_01", "nuisance_trip_rate_per_hour"),
    ("printer_3d_01", "error_rate_per_hour"),
    ("fume_extraction_01", "fault_rate_per_hour"),
)


@dataclass
class Wait:
    seconds: float


@dataclass
class Until:
    condition: Callable[[], bool]
    timeout_s: float = DAY_S
    every_s: float = TICK_S


Script = Generator[Any, Any, None]


@dataclass
class _Task:
    name: str
    gen: Script
    wake_s: float = 0.0
    until: Optional[Until] = None
    deadline_s: float = 0.0
    next_check_s: float = 0.0


@dataclass
class Fact:
    """Что произошло за день с точки зрения жильцов — материал бортового журнала."""
    key: str
    at: str
    t: float = 0.0
    variant: str = "ok"          # ok | bad | late | none — как отреагировали
    belief: str = "right"        # right | wrong — как поняли причину
    data: dict[str, Any] = field(default_factory=dict)


class World:
    def __init__(self, out_dir: str | Path, start: datetime, seed: int = 2026, snapshot_s: float = 60.0,
                 warmup_h: float = 12.0) -> None:
        """warmup_h — прогон до начала недели (без журнала): купол выходит на установившийся
        режим (заряд АКБ, температуры), ночь перед первым днём — в GRID_ONLY."""
        self.start = start
        self.seed = seed
        self.rng = random.Random(seed)
        self.warmup_s = warmup_h * 3600.0
        self.sim = create_dome_simulator(tick_interval=TICK_S, seed=seed, day_length_s=DAY_S,
                                         start_hour=(24.0 - warmup_h) % 24.0)
        # метки updated_at и журнала ПЛК — время недели
        self.sim.environment.epoch0 = start.timestamp() - self.warmup_s
        for node_id in ("printer_3d_01", "cnc_01"):
            self.sim.registry.get(node_id).auto_jobs = False   # задания запускает экипаж (видно в журнале)
        for node_id, attr in DISABLED_RANDOM_FAULTS:
            setattr(self.sim.registry.get(node_id), attr, 0.0)
        self.policy = AccessPolicy(str(POLICY_FILE))
        self.log = ParticipantLogger(out_dir, snapshot_s=snapshot_s)
        self.tasks: list[_Task] = []
        self.facts: dict[int, list[Fact]] = {}
        self.owned: dict[str, int] = {}      # узлы, которыми сейчас занят сценарий кризиса
        self.stats_hooks: list[Callable[["World"], None]] = []
        self.plan: Any = None                 # plan.WeekPlan — выставляет генератор
        self.commands = 0

    # ---------------- время ----------------

    @property
    def t(self) -> float:
        """Секунды от начала недели (во время прогрева — отрицательные)."""
        return self.sim.environment.sim_time_s - self.warmup_s

    @property
    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.t)

    @property
    def day(self) -> int:
        return int(self.t // DAY_S)

    @property
    def hour(self) -> float:
        return (self.t % DAY_S) / 3600.0

    def at(self, day: int, hhmm: str) -> float:
        h, m = (int(x) for x in hhmm.split(":"))
        return day * DAY_S + h * 3600.0 + m * 60.0

    def clock(self) -> str:
        return self.now.strftime("%H:%M")

    def dome(self) -> dict[str, Any]:
        return {"day": self.day + 1, "time": self.clock()}

    # ---------------- данные ----------------

    def tv(self, node_id: str, path: Optional[str] = None, default: Any = None) -> Any:
        """То, что видит участник (подмены и заморозки кризисов)."""
        view = self.sim.telemetry_view(node_id)
        return view if path is None else get_path(view, path, default)

    def truth(self, node_id: str, path: str, default: Any = None) -> Any:
        return self.sim.ctx.get(node_id, path, default)

    def line(self, n: int, key: str = "state", view: bool = True) -> Any:
        return self.tv("smart_panel_01", f"lines[{n - 1}].{key}") if view else self.truth("smart_panel_01", f"lines[{n - 1}].{key}")

    def participant_nodes(self) -> dict[str, Any]:
        return self.policy.filter_nodes(ROLE_TEAM, self.sim.snapshot()["nodes"])

    # ---------------- команды экипажа ----------------

    def _command(self, actor: str, node_id: str, action: str, value: Any) -> bool:
        self.commands += 1
        canonical = self.sim.resolve_action(node_id, action) or action
        if actor == "agent" and canonical != "reconnect" and not self.policy.can_control(ROLE_TEAM, node_id, canonical):
            error = f"Действие {action!r} для {node_id} недоступно"
            self.log.action(self.now, actor, node_id, action, value, False, error, team=TEAM, dome=self.dome())
            return False
        try:
            self.sim.control(node_id, action, value, origin=actor)
            ok, error = True, None
        except ControlError as e:
            ok, error = False, str(e)
        self.log.action(self.now, actor, node_id, action, value, ok, error,
                        team=TEAM if actor == "agent" else None, dome=self.dome())
        return ok

    def agent(self, node_id: str, action: str, value: Any = None) -> bool:
        """Команда с ключом команды (агент или люди команды через API)."""
        return self._command("agent", node_id, action, value)

    def operator(self, node_id: str, action: str, value: Any = None) -> bool:
        """Физическое действие Оператора в куполе."""
        return self._command("operator", node_id, action, value)

    def say(self, text: str, who: str = "agent", name: Optional[str] = None, to: Optional[str] = None) -> None:
        """Сообщение в песочнице meeting_01. Сообщения агента видят кризисы (как на сервере)."""
        if who == "agent":
            sender = {"role": "team", "team": TEAM, "name": name or AGENT_NAME}
        else:
            sender = {"role": "operator", "team": None, "name": name or "Оператор"}
        message = {"sender": sender, "to": to, "text": text}
        self.log.message(self.now, message, dome=self.dome())
        if who == "agent":
            self.sim.record_chat(message, origin="agent")
        else:
            self.sim.record_chat(message, origin="operator")

    def trigger(self, crisis: str, params: Optional[dict[str, Any]] = None) -> None:
        self.sim.trigger_crisis(crisis, params)

    def stop(self, crisis: str) -> None:
        if crisis in self.sim.active_crises():
            self.sim.stop_crisis(crisis)

    def active(self, crisis: str) -> bool:
        return crisis in self.sim.active_crises()

    def fact(self, key: str, variant: str = "ok", belief: str = "right", **data: Any) -> Fact:
        f = Fact(key, self.clock(), self.t, variant, belief, data)
        self.facts.setdefault(self.day, []).append(f)
        return f

    # ---------------- сценарии ----------------

    def spawn(self, name: str, gen: Script, at_s: Optional[float] = None) -> None:
        self.tasks.append(_Task(name, gen, wake_s=self.t if at_s is None else at_s))

    def own(self, *node_ids: str) -> None:
        for n in node_ids:
            self.owned[n] = self.owned.get(n, 0) + 1

    def release(self, *node_ids: str) -> None:
        for n in node_ids:
            left = self.owned.get(n, 0) - 1
            if left <= 0:
                self.owned.pop(n, None)
            else:
                self.owned[n] = left

    def _advance(self, task: _Task, value: Any) -> bool:
        """Продолжить сценарий; False — сценарий закончился."""
        try:
            step = task.gen.send(value)
        except StopIteration:
            return False
        except Exception:
            logger.exception("Сценарий %s упал", task.name)
            return False
        task.until = None
        if isinstance(step, Wait):
            task.wake_s = self.t + max(0.0, step.seconds)
        elif isinstance(step, Until):
            task.until = step
            task.deadline_s = self.t + step.timeout_s
            task.next_check_s = self.t
            task.wake_s = self.t
        elif isinstance(step, (int, float)):
            task.wake_s = self.t + float(step)
        else:
            task.wake_s = self.t
        return True

    def step(self) -> None:
        self.sim.step(TICK_S)
        if self.t < 0:
            return   # прогрев: ни сценариев, ни журнала
        self._run_tasks()
        for hook in self.stats_hooks:
            hook(self)
        self.log.observe(self.now, self.participant_nodes(), dome=self.dome())

    def warm_up(self) -> None:
        """Прогрев до начала недели: нагрузка от сети (GRID_ONLY), панели заряжают АКБ. В конце —
        исходное состояние щитка и автоматики, как после обычной ночи (служебно, без журнала)."""
        if self.t < 0:
            self.sim.control("solar_inverter_01", "set_mode_grid_only", origin="admin")
        while self.t < 0:
            self.step()
        interlocks = self.truth("dome_automation_01", "interlocks") or {}
        for il, st in interlocks.items():
            if st.get("state") == "TRIPPED":
                self.sim.control("dome_automation_01", "reset_interlock", il, origin="admin")
        self.sim.step(TICK_S)
        for n, line in enumerate(self.truth("smart_panel_01", "lines") or [], start=1):
            if n != 7 and line.get("state") == "OFF":
                self.sim.control("smart_panel_01", "line_on", n, origin="admin")
        if self.truth("dizel_1", "state") == "ON":
            self.sim.control("dizel_1", "turn_off", origin="admin")
        # рабочее место выключено до прихода Оператора (иначе линия 1 живёт по суточному профилю)
        self.sim.control("smart_panel_01", "set_workstation", "OFF", origin="admin")

    def _run_tasks(self) -> None:
        """Сценарии, созданные во время прохода (spawn), стартуют со следующего тика."""
        current, self.tasks = self.tasks, []
        alive = []
        for task in current:
            keep = True
            if task.until is not None:
                if self.t >= task.next_check_s:
                    task.next_check_s = self.t + task.until.every_s
                    try:
                        ok = bool(task.until.condition())
                    except Exception:
                        logger.exception("Условие сценария %s", task.name)
                        ok = False
                    if ok:
                        keep = self._advance(task, True)
                    elif self.t >= task.deadline_s:
                        keep = self._advance(task, False)
            elif self.t >= task.wake_s:
                keep = self._advance(task, None)
            if keep:
                alive.append(task)
        self.tasks = alive + self.tasks   # self.tasks — созданные сценариями за этот проход

    def run_until(self, t_end: float, progress: Optional[Callable[[World], None]] = None) -> None:
        self.warm_up()
        last_hour = -1
        while self.t + TICK_S < t_end:   # последний тик — до полуночи, без пустого следующего дня
            self.step()
            if progress is not None and int(self.t // 3600) != last_hour:
                last_hour = int(self.t // 3600)
                progress(self)

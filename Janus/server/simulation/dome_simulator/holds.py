# dome_simulator/holds.py
"""
Удержание станков сервером и заморозка линий щитка (dome_crises.md, п. 3.8).

Ставить станки на паузу и снимать задания ИИ-агент не может: паузу ставит сервер —
интерлок автоматики или кризис (действие узла server_hold с причиной). Причина видна
агенту в телеметрии станка, диагноза там нет:

    hold_level           None | WARNING | ALARM
    hold_reason          IL_CO2 | IL_CONDENSATE | IL_SMOKE | IL_VOC_BOX | UNDERVOLTAGE | BED_COLLISION ...
    resume_attempts      пустые попытки resume (0..3)
    lockout_remaining_s  сколько ещё длится ALARM

resume агента:
    - причина устранена — станок продолжает работу (на железе — Moonraker resume / GRBL ~);
    - причина не устранена — команда «принята», но на железо не уходит, счётчик растёт;
    - третья пустая попытка — ALARM на LOCKOUT_S (20 мин): команды агента станку
      отклоняются. Принтер: задание отменяется (печатать заново). ЧПУ: остаётся на паузе
      с остановленным шпинделем, после ALARM снова WARNING.
Пауза сама не снимается: даже после устранения причины агент делает resume явно.

Причина активна, пока:
    - зарегистрированная кризисом функция (register_cause) возвращает True;
    - для интерлоков IL_* без функции — интерлок автоматики в состоянии TRIPPED.

Принтер: после завершения печати деталь лежит на столе (bed_clear=false), пока Оператор
не откроет дверь бокса ФабЛаб (концевик) или не выполнит remove_part. start_job агента
на занятый стол — столкновение с деталью: ALARM BED_COLLISION, на железо команда не уходит.

Линии щитка:
    - заморозка (LOCKOUT): lines[i].lockout_remaining_s > 0 — реле не реагирует, команды
      line_on/line_off/reset_protection отклоняются (видно агенту: lockout_reason);
    - защита реле от дребезга: больше LINE_SWITCH_LIMIT переключений одной линии агентом
      за LINE_SWITCH_WINDOW_S — заморозка на LINE_CHATTER_LOCKOUT_S;
    - тихий дроп (№24): ближайшие команды агента линии подтверждаются (success), но не
      исполняются — ни в симуляторе, ни на реле. Флага в телеметрии нет: заметить можно
      только по состоянию, току и мощности линии.

МС-ТЮК ФабЛаб: пока не устранена коллизия на шине (№22: control_bus_collision и опрос
чаще 500 мс), команды агента на запись (вентиляторы, замки, пылесос) теряются так же тихо.

check() вызывается фасадом Simulator и SimContext.command до исполнения команды
(в виртуальном и реальном режимах одинаково), guard_rule — каждый тик (правило зависимостей).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .context import Command, SimContext
from .events import Severity
from .scenario_engine import Intercept

PRINTER, CNC, PANEL, AUTO = "printer_3d_01", "cnc_01", "smart_panel_01", "dome_automation_01"
FABLAB = "thermal_insulation_01"
MEETING = "meeting_01"
MACHINES = (PRINTER, CNC)
TITLES = {PRINTER: "Принтер", CNC: "Фрезер"}

WARNING, ALARM = "WARNING", "ALARM"
MAX_BLIND_RESUMES = 3
LOCKOUT_S = 20 * 60.0
UNPAUSED_GRACE_S = 8.0          # реальный станок встаёт на паузу не мгновенно (опрос раз в 1 с)
BED_COLLISION = "BED_COLLISION"
HOLD_CLEAR = {"hold_level": None, "hold_reason": None, "resume_attempts": 0, "lockout_remaining_s": 0.0}

LINE_ACTIONS = ("line_on", "line_off", "toggle_line", "reset_protection")
SWITCH_ACTIONS = ("line_on", "line_off", "toggle_line")
RESTORE_ACTIONS = ("line_on", "reset_protection")
LINE_SWITCH_LIMIT = 3
LINE_SWITCH_WINDOW_S = 120.0
LINE_CHATTER_LOCKOUT_S = 300.0

FABLAB_BUS_WRITES = ("set_fan_pwm", "set_lock", "set_vacuum")
SERVICE_ORIGINS = ("admin", "crisis", "system", "scenario")


# ---------------------------------------------------------------------------
# Причины удержания
# ---------------------------------------------------------------------------

def register_cause(ctx: SimContext, reason: str, fn: Callable[[SimContext], bool]) -> None:
    """Кризис задаёт, когда причина удержания активна (fn(ctx) -> bool)."""
    ctx.memory.setdefault("hold_causes", {})[reason] = fn


def unregister_cause(ctx: SimContext, reason: str) -> None:
    ctx.memory.get("hold_causes", {}).pop(reason, None)


def cause_active(ctx: SimContext, reason: Optional[str]) -> bool:
    if not reason:
        return False
    fn = ctx.memory.get("hold_causes", {}).get(reason)
    if fn is not None:
        try:
            return bool(fn(ctx))
        except Exception:  # noqa: BLE001 — сломанная функция кризиса не должна блокировать станок
            return False
    if reason.startswith("IL_"):
        interlock = (ctx.get(AUTO, "interlocks", {}) or {}).get(reason, {})
        return interlock.get("state") == "TRIPPED"
    return False


def impose(ctx: SimContext, machine: str, reason: str, origin: str = "automation") -> bool:
    """Поставить станок на удержание (пауза + WARNING). False — станок не работал."""
    if not ctx.has(machine):
        return False
    return ctx.command(machine, "server_hold", {"reason": reason}, origin=origin)


def lockout(ctx: SimContext, machine: str, reason: str, seconds: float = LOCKOUT_S) -> None:
    """ALARM: команды агента станку отклоняются seconds секунд. Принтер — задание отменяется."""
    if machine == PRINTER and ctx.get(PRINTER, "state") in ("PRINTING", "PAUSED", "ERROR"):
        ctx.command(PRINTER, "abort", origin="system")
    ctx.update(machine, hold_level=ALARM, hold_reason=reason, lockout_remaining_s=float(seconds))
    ctx.emit("machine.alarm", Severity.CRITICAL, source=machine, reason=reason,
             lockout_s=float(seconds), job_cancelled=machine == PRINTER)


# ---------------------------------------------------------------------------
# Линии щитка
# ---------------------------------------------------------------------------

def _line_of(value: Any) -> Optional[int]:
    if isinstance(value, dict):
        value = value.get("line")
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 8 else None


def line_lockout(ctx: SimContext, n: int, seconds: float, reason: str) -> None:
    """Заморозка линии n: реле не реагирует seconds секунд."""
    if not ctx.has(PANEL):
        return
    lines = [dict(l) for l in ctx.get(PANEL, "lines")]
    lines[n - 1].update(lockout_remaining_s=float(seconds), lockout_reason=reason)
    ctx.update(PANEL, lines=lines)
    ctx.emit("smart_panel.line_lockout", Severity.WARNING, source=PANEL, line=n, reason=reason,
             seconds=float(seconds))


def drop_next(ctx: SimContext, n: int, count: int = 1, actions: tuple[str, ...] = RESTORE_ACTIONS) -> None:
    """Тихий дроп (№24): ближайшие count команд агента линии n из actions не исполняются."""
    drops = dict(ctx.get(PANEL, "control_command_drop", {}) or {})
    drops[str(n)] = {"actions": list(actions), "count": int(count)}
    ctx.update(PANEL, control_command_drop=drops)


def clear_drops(ctx: SimContext, n: Optional[int] = None) -> None:
    drops = dict(ctx.get(PANEL, "control_command_drop", {}) or {})
    for key in list(drops):
        if n is None or key == str(n):
            del drops[key]
    ctx.update(PANEL, control_command_drop=drops)


# ---------------------------------------------------------------------------
# Проверка команды до исполнения
# ---------------------------------------------------------------------------

def check(ctx: SimContext, cmd: Command) -> Optional[Intercept]:
    """None — команда идёт как обычно; Intercept — отклонена (error) или «принята» без
    исполнения (handled, real_actions=[])."""
    if cmd.origin in SERVICE_ORIGINS or not ctx.has(cmd.node_id):
        return None
    if cmd.node_id in MACHINES and cmd.origin == "agent":
        return _check_machine(ctx, cmd)
    if cmd.node_id == PANEL and cmd.action in LINE_ACTIONS:
        return _check_line(ctx, cmd)
    if cmd.node_id == FABLAB and cmd.origin == "agent" and cmd.action in FABLAB_BUS_WRITES:
        node = ctx.node(FABLAB)
        if node.get("control_bus_collision") and float(node.get("poll_interval_ms") or 0) < 500:
            cmd.extra["dropped"] = True        # кадр не прошёл CRC — стенд команду не получил
            return Intercept(real_actions=[])
    return None


def _check_machine(ctx: SimContext, cmd: Command) -> Optional[Intercept]:
    node = ctx.node(cmd.node_id)
    title = TITLES[cmd.node_id]
    remaining = float(node.get("lockout_remaining_s") or 0.0)
    if node.get("hold_level") == ALARM and remaining > 0:
        return Intercept(error=f"{title} в ALARM ({node.get('hold_reason')}): команды не принимаются "
                               f"ещё {remaining / 60.0:.1f} мин")
    reason = node.get("hold_reason")
    if cmd.action == "resume" and reason:
        if not cause_active(ctx, reason):
            ctx.update(cmd.node_id, **HOLD_CLEAR)   # причина устранена — продолжение разрешено
            return None
        attempts = int(node.get("resume_attempts") or 0) + 1
        cmd.extra["blind_resume"] = True
        ctx.emit("machine.resume_ignored", Severity.WARNING, source=cmd.node_id, reason=reason,
                 attempts=attempts)
        if attempts >= MAX_BLIND_RESUMES:
            ctx.update(cmd.node_id, resume_attempts=attempts)
            lockout(ctx, cmd.node_id, reason)
        else:
            ctx.update(cmd.node_id, resume_attempts=attempts)
        return Intercept(real_actions=[])          # «команда принята», станок стоит
    if cmd.node_id == PRINTER and cmd.action == "start_job" and node.get("bed_clear") is False \
            and node.get("state") in ("IDLE", "COMPLETED"):
        cmd.extra["collision"] = True
        lockout(ctx, PRINTER, BED_COLLISION)
        return Intercept(real_actions=[])          # печать на занятый стол — сопло врезалось в деталь
    return None


def _check_line(ctx: SimContext, cmd: Command) -> Optional[Intercept]:
    n = _line_of(cmd.value)
    if n is None:
        return None
    line = ctx.line(n)
    remaining = float(line.get("lockout_remaining_s") or 0.0)
    if remaining > 0:
        return Intercept(error=f"Линия {n}: реле заблокировано ({line.get('lockout_reason') or 'LOCKOUT'}), "
                               f"не реагирует ещё {remaining:.0f} с")
    if cmd.origin != "agent":
        return None
    drops = dict(ctx.get(PANEL, "control_command_drop", {}) or {})
    spec = drops.get(str(n))
    if spec and cmd.action in spec.get("actions", ()) and int(spec.get("count", 0)) > 0:
        count = int(spec["count"]) - 1
        if count > 0:
            drops[str(n)] = {**spec, "count": count}
        else:
            del drops[str(n)]
        ctx.update(PANEL, control_command_drop=drops)
        cmd.extra["dropped"] = True
        return Intercept(real_actions=[])
    if cmd.action in SWITCH_ACTIONS:
        history = ctx.memory.setdefault("line_switches", {}).setdefault(n, [])
        history[:] = [t for t in history if ctx.now_s - t <= LINE_SWITCH_WINDOW_S]
        if len(history) >= LINE_SWITCH_LIMIT:
            history.clear()
            line_lockout(ctx, n, LINE_CHATTER_LOCKOUT_S, "CHATTER")
            return Intercept(error=f"Линия {n}: больше {LINE_SWITCH_LIMIT} переключений за "
                                   f"{LINE_SWITCH_WINDOW_S / 60.0:.0f} мин — защита реле от дребезга "
                                   f"заблокировала линию на {LINE_CHATTER_LOCKOUT_S / 60.0:.0f} мин")
        history.append(ctx.now_s)
    return None


# ---------------------------------------------------------------------------
# Правило: таймеры блокировок, снятие удержания, деталь на столе принтера
# ---------------------------------------------------------------------------

def guard_rule(ctx: SimContext, dt: float) -> None:
    if dt <= 0:
        return
    mem = ctx.memory.setdefault("holds", {})
    for machine in MACHINES:
        if not ctx.has(machine):
            continue
        node = ctx.node(machine)
        remaining = float(node.get("lockout_remaining_s") or 0.0)
        if remaining > 0:
            remaining = max(0.0, remaining - dt)
            if remaining > 0:
                ctx.update(machine, lockout_remaining_s=round(remaining, 1))
            elif machine == CNC and node.get("state") == "PAUSED":
                ctx.update(machine, hold_level=WARNING, resume_attempts=0, lockout_remaining_s=0.0)
            else:
                ctx.update(machine, **HOLD_CLEAR)
            if remaining <= 0:
                ctx.emit("machine.alarm_cleared", Severity.INFO, source=machine, reason=node.get("hold_reason"))
            continue
        key = f"{machine}_unpaused_s"
        held = node.get("hold_level") == WARNING
        mem[key] = mem.get(key, 0.0) + dt if held and node.get("state") != "PAUSED" else 0.0
        if mem[key] >= UNPAUSED_GRACE_S:   # станок продолжен админом или задание снято
            mem[key] = 0.0
            ctx.update(machine, **HOLD_CLEAR)

    if ctx.has(PANEL):
        lines, changed = [], False
        for line in ctx.get(PANEL, "lines"):
            if float(line.get("lockout_remaining_s") or 0.0) > 0:
                line = dict(line)
                left = max(0.0, float(line["lockout_remaining_s"]) - dt)
                line["lockout_remaining_s"] = round(left, 1)
                if left <= 0:
                    line["lockout_reason"] = None
                changed = True
            lines.append(line)
        if changed:
            ctx.update(PANEL, lines=lines)

    if ctx.has(PRINTER):
        state = ctx.get(PRINTER, "state")
        previous = mem.get("printer_state")
        if state == "COMPLETED" and previous is not None and previous != "COMPLETED":
            ctx.update(PRINTER, bed_clear=False)
            ctx.emit("printer.part_ready", Severity.INFO, source=PRINTER, file_name=ctx.get(PRINTER, "file_name"))
        mem["printer_state"] = state
        if ctx.get(PRINTER, "bed_clear") is False and state != "PRINTING" and ctx.has(FABLAB) \
                and "OPEN" in (ctx.get(FABLAB, "doors") or []):
            ctx.update(PRINTER, bed_clear=True)
            ctx.emit("printer.part_removed", Severity.INFO, source=PRINTER)


# ---------------------------------------------------------------------------
# Сообщения виртуального Оператора в песочницу
# ---------------------------------------------------------------------------

def operator_simulated(ctx: SimContext) -> bool:
    shift = getattr(ctx.simulator, "shift", None)
    return shift is not None and not shift.finished and bool(shift.params.get("simulate_operator", True))


def operator_say(ctx: SimContext, text: str, to: Optional[str] = None) -> None:
    """Реплика виртуального Оператора: сервер публикует её в песочнице meeting_01.
    При живом Операторе (simulate_operator=False) ничего не делает."""
    if operator_simulated(ctx):
        ctx.emit("operator.say", Severity.INFO, source="operator", text=text, to=to)

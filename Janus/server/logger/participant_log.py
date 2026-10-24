# logger/participant_log.py
"""
Журнал участника — то, что видит команда, в JSONL-файлах по дням (для RAG агента).

Пишется только то, что доступно ключу команды: телеметрия после фильтра политики
доступа (authorization/access_policy.json) с подменами показаний кризисов, команды
участника и их ответы, физические действия Оператора, сообщения песочницы meeting_01.
Истинного состояния, имён кризисов и отчётов в журнале нет.

Каталог журнала:
    <root>/<YYYY-MM-DD>/telemetry.jsonl   снимок телеметрии всех узлов раз в snapshot_s
    <root>/<YYYY-MM-DD>/events.jsonl      по времени: change, automation, journal, action, message

Записи (у всех: ts — локальное время ISO 8601, dome — часы купола {"day", "time"}):
    {"kind": "telemetry", "nodes": {...}}
    {"kind": "change", "node_id", "param", "from", "to"}   дискретные поля: состояния, флаги, режимы
    {"kind": "automation", "node_id": "dome_automation_01", "event": {...}}   новая запись журнала ПЛК
    {"kind": "journal", "node_id", "param", "entry": {...}}  новая запись других журналов узлов
    {"kind": "action", "actor": "agent" | "operator", "team", "node_id", "action", "value",
     "success", "error"}
    {"kind": "message", "sender": {...}, "to", "text"}

Числовые показания меняются каждый тик — они только в снимках telemetry. В change попадают
строки, флаги и None (в том числе пропадание числового показания); частые колебания
(NOISY_PARAMS) не пишутся — они видны в снимках.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("ParticipantLog")

# Поля, которые не сравниваются: метки времени и журналы (журналы — отдельными записями)
SKIP_PARAMS = {"updated_at"}
JOURNAL_PARAMS = {("dome_automation_01", "events"), ("access_control_01", "passage_events")}
SKIP_NODES = {"meeting_01"}   # сообщения песочницы пишутся записями message
# Дискретные поля, которые штатно переключаются каждые несколько минут (видны в снимках):
# (узел, шаблон поля) -> штатные значения. Переход между штатными значениями не пишется,
# переход в любое другое значение (FAULT, FORCED, ...) — пишется. None — поле не пишется вовсе.
NOISY_PARAMS: dict[tuple[str, str], Optional[frozenset]] = {
    ("battery_01", "state"): None,
    ("battery_01", "balancing_state"): None,
    ("solar_inverter_01", "mppt_state"): frozenset({"TRACKING", "LIMITED", "IDLE"}),
    ("solar_inverter_01", "fan_state"): frozenset({"ON", "OFF"}),
    ("water_pump_01", "running"): None,
    ("air_quality_sensor_01", "warming_up"): None,
    ("weather_station_01", "wind_direction"): None,
    ("radio_01", "reception"): None,
    ("radio_01", "state"): frozenset({"RECEIVING", "NO_SIGNAL"}),
    ("backup_comms_01", "state"): frozenset({"AVAILABLE", "DEGRADED"}),
    ("access_control_01", "doors.*.state"): frozenset({"OPEN", "CLOSED"}),
}
MAX_VALUE_CHARS = 2000     # длинные значения команд (G-code в upload_file) обрезаются
NUMBER_STABLE_N = 5        # пропадание/появление числового показания — если держится столько опросов подряд


def _is_discrete(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool))


def _scalar_list(value: Any) -> bool:
    return isinstance(value, list) and all(_is_discrete(v) for v in value)


def flatten(node: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Пути дискретных полей узла: 'state', 'lines[0].state', 'interlocks.IL_CO2.state'.
    Списки строк/флагов (locks, doors) сравниваются целиком. Числа — тоже, но только
    чтобы отследить пропадание показания (значение None)."""
    out: dict[str, Any] = {}
    for key, value in node.items():
        path = f"{prefix}{key}"
        if not prefix and key in SKIP_PARAMS:
            continue
        if isinstance(value, dict):
            out.update(flatten(value, path + "."))
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            for i, item in enumerate(value):
                out.update(flatten(item, f"{path}[{i}]."))
        elif _is_discrete(value) or _scalar_list(value):
            out[path] = value
        elif isinstance(value, (int, float)):
            out[path] = _NUMBER
    return out


class _Number:
    """Маркер «числовое значение есть» — изменение числа не событие, пропадание (None) — событие."""

    def __repr__(self) -> str:
        return "<number>"


_NUMBER = _Number()


_ALWAYS_QUIET = frozenset({"*"})
_quiet_cache: dict[tuple[str, str], Optional[frozenset]] = {}


def _quiet_values(node_id: str, param: str) -> Optional[frozenset]:
    """Штатные значения поля; _ALWAYS_QUIET — поле не пишется; None — пишется любое изменение."""
    key = (node_id, param)
    if key not in _quiet_cache:
        found: Optional[frozenset] = None
        for (n, pattern), values in NOISY_PARAMS.items():
            if n == node_id and fnmatch.fnmatchcase(param, pattern):
                found = _ALWAYS_QUIET if values is None else values
                break
        _quiet_cache[key] = found
    return _quiet_cache[key]


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, _Number) or isinstance(b, _Number):
        return isinstance(a, _Number) and isinstance(b, _Number)
    return a == b


def _entry_key(entry: Any) -> str:
    if isinstance(entry, dict) and entry.get("event_id"):
        return str(entry["event_id"])
    return json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)


def _clip(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return {**value, "content": f"<{len(value['content'])} символов>"}
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + "…"
    return value


class ParticipantLogger:
    """
    root — каталог журнала; snapshot_s — период снимков телеметрии (секунды по часам ts).
    Время записей передаёт вызывающий код (now: datetime), часы купола — dome={"day", "time"}:
    сервер пишет по системным часам, генератор недельного лога — по часам симуляции.
    Потокобезопасен: сервер вызывает из цикла событий, генератор — из одного потока.
    """

    def __init__(self, root: str | Path, snapshot_s: float = 60.0) -> None:
        self.root = Path(root)
        self.snapshot_s = float(snapshot_s)
        self._lock = threading.Lock()
        self._last_snapshot: Optional[datetime] = None
        self._prev: dict[str, dict[str, list[Any]]] = {}   # node -> param -> [записанное, кандидат, повторов]
        self._journals: dict[tuple[str, str], dict[str, str]] = {}
        self._files: dict[tuple[str, str], Any] = {}
        self._day: Optional[str] = None

    # ---------------- файлы ----------------

    def _file(self, now: datetime, stream: str):
        day = now.strftime("%Y-%m-%d")
        if day != self._day:
            self._close_files()
            self._day = day
        key = (day, stream)
        f = self._files.get(key)
        if f is None:
            folder = self.root / day
            folder.mkdir(parents=True, exist_ok=True)
            # построчная запись: при аварийной остановке сервера журнал не теряется
            f = open(folder / f"{stream}.jsonl", "a", encoding="utf-8", buffering=1)
            self._files[key] = f
        return f

    def _write(self, now: datetime, stream: str, record: dict[str, Any]) -> None:
        try:
            f = self._file(now, stream)
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        except OSError as e:
            logger.error("Журнал участника: запись не удалась: %s", e)

    def _close_files(self) -> None:
        for f in self._files.values():
            try:
                f.close()
            except OSError:
                pass
        self._files.clear()

    def flush(self) -> None:
        with self._lock:
            for f in self._files.values():
                f.flush()

    def close(self) -> None:
        with self._lock:
            self._close_files()

    @staticmethod
    def _head(now: datetime, dome: Optional[dict[str, Any]], kind: str) -> dict[str, Any]:
        record = {"ts": now.isoformat(timespec="seconds"), "kind": kind}
        if dome is not None:
            record["dome"] = dome
        return record

    # ---------------- телеметрия ----------------

    def observe(self, now: datetime, nodes: dict[str, dict[str, Any]], dome: Optional[dict[str, Any]] = None,
                force_snapshot: bool = False) -> None:
        """Телеметрия участника (уже отфильтрованная политикой). Вызывается часто (каждый
        тик / каждые несколько секунд): изменения пишутся сразу, снимок — раз в snapshot_s."""
        with self._lock:
            self._diff(now, nodes, dome)
            due = (self._last_snapshot is None or force_snapshot
                   or (now - self._last_snapshot).total_seconds() >= self.snapshot_s
                   or now.date() != self._last_snapshot.date())
            if due:
                self._last_snapshot = now
                self._write(now, "telemetry", {**self._head(now, dome, "telemetry"), "nodes": nodes})

    def _diff(self, now: datetime, nodes: dict[str, dict[str, Any]], dome: Optional[dict[str, Any]]) -> None:
        for node_id, node in nodes.items():
            if node_id in SKIP_NODES or not isinstance(node, dict):
                continue
            for param in [p for (n, p) in JOURNAL_PARAMS if n == node_id]:
                self._journal(now, dome, node_id, param, node.get(param))
            flat = flatten({k: v for k, v in node.items() if (node_id, k) not in JOURNAL_PARAMS})
            state = self._prev.setdefault(node_id, {})
            for param, value in flat.items():
                quiet = _quiet_values(node_id, param)
                if quiet is _ALWAYS_QUIET:
                    continue
                st = state.get(param)
                if st is None:   # первое наблюдение поля — исходное состояние есть в снимке
                    state[param] = [value, value, 0]
                    continue
                emitted, candidate, count = st
                if _same(value, emitted):
                    st[1], st[2] = value, 0
                    continue
                count = count + 1 if _same(value, candidate) else 1
                numeric = isinstance(value, _Number) or isinstance(emitted, _Number)
                if count < (NUMBER_STABLE_N if numeric else 1):
                    st[1], st[2] = value, count
                    continue
                st[:] = [value, value, 0]
                if quiet is not None and emitted in quiet and value in quiet:
                    continue
                record = {**self._head(now, dome, "change"), "node_id": node_id, "param": param,
                          "from": None if isinstance(emitted, _Number) else emitted,
                          "to": None if isinstance(value, _Number) else value}
                if isinstance(emitted, _Number):
                    record["note"] = "показание пропало"
                elif isinstance(value, _Number):
                    record["note"] = "показание появилось"
                self._write(now, "events", record)

    def _journal(self, now: datetime, dome: Optional[dict[str, Any]], node_id: str, param: str, entries: Any) -> None:
        """Новые записи журналов узлов. Запись с event_id (журнал ПЛК) может измениться
        (квитирование) — тогда она пишется повторно с "updated": true."""
        if not isinstance(entries, list):
            return
        key = (node_id, param)
        current = {_entry_key(e): json.dumps(e, ensure_ascii=False, sort_keys=True, default=str) for e in entries}
        seen = self._journals.get(key)
        self._journals[key] = current
        if seen is None:
            return   # записи до начала журнала есть в первом снимке
        kind = "automation" if node_id == "dome_automation_01" else "journal"
        for entry in entries:
            k = _entry_key(entry)
            if seen.get(k) == current[k]:
                continue
            record = {**self._head(now, dome, kind), "node_id": node_id}
            if kind == "journal":
                record["param"] = param
            record["event" if kind == "automation" else "entry"] = entry
            if k in seen:
                record["updated"] = True
            self._write(now, "events", record)

    # ---------------- действия и сообщения ----------------

    def action(self, now: datetime, actor: str, node_id: Any, action: Any, value: Any, success: bool,
               error: Optional[str] = None, team: Optional[int] = None,
               dome: Optional[dict[str, Any]] = None) -> None:
        """Команда участника (actor="agent") или физическое действие Оператора (actor="operator")."""
        record = {**self._head(now, dome, "action"), "actor": actor}
        if team is not None:
            record["team"] = team
        record.update({"node_id": node_id, "action": action, "value": _clip(value), "success": bool(success)})
        if error:
            record["error"] = error
        with self._lock:
            self._write(now, "events", record)

    def message(self, now: datetime, message: dict[str, Any], dome: Optional[dict[str, Any]] = None) -> None:
        """Сообщение песочницы meeting_01 (отправитель проставлен сервером)."""
        record = {**self._head(now, dome, "message"), "sender": message.get("sender"),
                  "to": message.get("to"), "text": message.get("text")}
        with self._lock:
            self._write(now, "events", record)

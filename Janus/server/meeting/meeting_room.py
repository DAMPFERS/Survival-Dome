"""
Песочница (узел meeting_01) — общий чат на сервере купола.

Пространство, через которое ИИ-агенты разных команд и Оператор в куполе
обмениваются сообщениями: добавить сообщение и прочитать предыдущие.
Живёт на сервере, вне симуляции купола: кризисы связи на чат не влияют,
сброс мира при старте смены историю не трогает.

История хранится в JSON Lines (одна строка — одно сообщение) и переживает
перезапуск сервера. Очистка (clear, только админ) переписывает файл одной
служебной строкой {"cleared": true, "last_id": N}: нумерация сообщений
продолжается, клиенты, читающие с since_id, ничего не теряют.

Сообщение:
    {"id": 17, "time": 1760000000.0,
     "sender": {"role": "team" | "guest" | "operator", "team": 2 | null, "name": "power-agent" | null},
     "to": "team1" | null, "text": "..."}
Отправителя (role, team) проставляет сервер по сессии; name и to — от клиента.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("MeetingRoom")

NODE_ID = "meeting_01"
CODE_NAME = "meeting"
TITLE = "Песочница"
SYSTEM = "Песочница"

MAX_TEXT = 4000           # символов в сообщении
MAX_NAME = 64             # имя агента / адресат
MAX_MEMORY = 5000         # сообщений в памяти (файл хранит всё до clear)
MAX_READ = 200            # сообщений за один запрос read_messages
RECENT_IN_TELEMETRY = 10


class MeetingError(ValueError):
    """Некорректный запрос к песочнице — текст уходит клиенту."""


def _short_text(value: Any, what: str, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MeetingError(f"{what}: ожидалась строка")
    value = value.strip()
    if len(value) > limit:
        raise MeetingError(f"{what}: не длиннее {limit} символов")
    return value or None


class MeetingRoom:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._messages: deque[dict[str, Any]] = deque(maxlen=MAX_MEMORY)
        self._last_id = 0
        self._load()

    # ---------------- хранение ----------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        broken = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("cleared"):
                        self._messages.clear()
                        self._last_id = max(self._last_id, int(record.get("last_id", 0)))
                        continue
                    self._last_id = max(self._last_id, int(record["id"]))
                    self._messages.append(record)
                except (ValueError, KeyError, TypeError, AttributeError):
                    broken += 1  # недописанная строка (сбой питания) — пропускаем
        if broken:
            logger.warning("Песочница: пропущено повреждённых строк истории: %d", broken)
        logger.info("Песочница: загружено сообщений %d (последний id %d) из %s",
                    len(self._messages), self._last_id, self.path)

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---------------- API ----------------

    def post(self, sender: dict[str, Any], value: Any) -> dict[str, Any]:
        """value: текст сообщения или {"text": ..., "name"?: имя агента, "to"?: адресат}."""
        if isinstance(value, str):
            value = {"text": value}
        if not isinstance(value, dict):
            raise MeetingError('post_message: ожидался текст или {"text": ..., "name"?: ..., "to"?: ...}')
        text = _short_text(value.get("text"), "text", MAX_TEXT)
        if not text:
            raise MeetingError("post_message: пустое сообщение")
        sender = {**sender, "name": _short_text(value.get("name"), "name", MAX_NAME)}
        to = _short_text(value.get("to"), "to", MAX_NAME)
        with self._lock:
            self._last_id += 1
            message = {"id": self._last_id, "time": round(time.time(), 3), "sender": sender, "to": to, "text": text}
            try:
                self._append(message)
            except OSError as e:
                logger.error("Песочница: сообщение %d не сохранено в файл: %s", self._last_id, e)
            self._messages.append(message)
        return message

    def read(self, params: Any = None) -> list[dict[str, Any]]:
        """params: {"since_id"?: N — сообщения с id > N, "limit"?: 1..200 (по умолчанию 50)}.
        Возвращает последние limit сообщений из подходящих, по возрастанию id."""
        params = params if isinstance(params, dict) else {}
        try:
            since = int(params.get("since_id") or 0)
            limit = int(params.get("limit") or 50)
        except (TypeError, ValueError):
            raise MeetingError("read_messages: since_id и limit — целые числа") from None
        if not 1 <= limit <= MAX_READ:
            raise MeetingError(f"read_messages: limit в диапазоне 1..{MAX_READ}")
        with self._lock:
            selected = [m for m in self._messages if m["id"] > since]
        return selected[-limit:]

    def clear(self) -> int:
        """Очищает историю (админ). Возвращает число удалённых сообщений."""
        with self._lock:
            removed = len(self._messages)
            self._messages.clear()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps({"cleared": True, "last_id": self._last_id, "time": time.time()}) + "\n")
            os.replace(tmp, self.path)
        return removed

    def summary(self) -> dict[str, Any]:
        """Телеметрия узла meeting_01."""
        with self._lock:
            recent = list(self._messages)[-RECENT_IN_TELEMETRY:]
            count = len(self._messages)
            last_id = self._last_id
        return {"online": True, "message_count": count, "last_message_id": last_id, "recent_messages": recent}

    @staticmethod
    def describe() -> dict[str, Any]:
        """Описание узла в формате describe_nodes симулятора."""
        return {
            "node_id": NODE_ID, "code_name": CODE_NAME, "title": TITLE, "system": SYSTEM, "category": "sandbox",
            "controls": ["clear", "post_message"],
            "control_aliases": {"clear": [], "post_message": ["post", "send_message"]},
            "operator_controls": [],
            "queries": ["read_messages"],
            "service_actions": [],
        }

    @staticmethod
    def resolve_action(action: str) -> Optional[str]:
        action = action.strip().lower()
        for name, aliases in MeetingRoom.describe()["control_aliases"].items():
            if action == name or action in aliases:
                return name
        return None

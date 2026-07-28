"""Потокобезопасная общая шина сообщений мультиагентной системы."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ChatMessage:
    speaker: str
    content: str
    is_event: bool = False
    timestamp: float = field(default_factory=time.time)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    root_id: Optional[str] = None
    depth: int = 0

    def __post_init__(self) -> None:
        if self.root_id is None:
            self.root_id = self.message_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "speaker": self.speaker,
            "content": self.content,
            "is_event": self.is_event,
            "timestamp": self.timestamp,
            "message_id": self.message_id,
            "root_id": self.root_id,
            "depth": self.depth,
        }


class SharedChatLog:
    """Единая история чата с безопасными подписчиками на новые сообщения."""

    def __init__(self, log_file: Optional[str] = None):
        self._messages: List[ChatMessage] = []
        self._lock = threading.RLock()
        self._log_file = log_file
        self._subscribers: List[Callable[[ChatMessage], None]] = []

    def subscribe(self, callback: Callable[[ChatMessage], None]) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[ChatMessage], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def append(
        self,
        speaker: str,
        content: str,
        is_event: bool = False,
        *,
        root_id: Optional[str] = None,
        depth: int = 0,
    ) -> ChatMessage:
        msg = ChatMessage(
            speaker=speaker,
            content=content,
            is_event=is_event,
            root_id=root_id,
            depth=max(0, depth),
        )
        with self._lock:
            self._messages.append(msg)
            subscribers = list(self._subscribers)
            if self._log_file:
                self._dump_to_file_locked()

        # Колбэки вызываются вне lock: медленный браузер не блокирует чат.
        for callback in subscribers:
            try:
                callback(msg)
            except Exception:
                # Шина не должна падать из-за внешнего потребителя.
                continue
        return msg

    def since(self, index: int) -> List[ChatMessage]:
        with self._lock:
            return list(self._messages[index:])

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)

    def build_context(
        self,
        agent_name: str,
        system_prompt: str,
        history_limit: int = 40,
    ) -> List[Dict[str, str]]:
        with self._lock:
            tail = self._messages[-history_limit:]

        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for msg in tail:
            if msg.speaker == agent_name:
                messages.append({"role": "assistant", "content": msg.content})
            else:
                prefix = "[СОБЫТИЕ]" if msg.is_event else f"[{msg.speaker}]"
                messages.append({"role": "user", "content": f"{prefix} {msg.content}"})
        return messages

    def _dump_to_file_locked(self) -> None:
        try:
            with open(self._log_file, "w", encoding="utf-8") as stream:
                json.dump(
                    [message.to_dict() for message in self._messages],
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError:
            pass

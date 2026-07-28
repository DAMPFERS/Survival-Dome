"""
Общий чат агентов: единая история сообщений, видимая всем участникам.

Каждый агент строит из неё СВОЙ список messages для вызова LLM API:
- свои же прошлые реплики -> role="assistant"
- чужие реплики (других агентов/оператора/кризисных событий) -> role="user"
  с префиксом [Имя] или [СОБЫТИЕ], чтобы модель понимала, кто это сказал
  (LLM API не поддерживает "многосторонние" роли, это стандартный приём
  для реализации multi-agent чата поверх single-agent API).
"""

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ChatMessage:
    speaker: str
    content: str
    is_event: bool = False       # True для кризисных/системных событий
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "speaker": self.speaker,
            "content": self.content,
            "is_event": self.is_event,
            "timestamp": self.timestamp,
        }


class SharedChatLog:
    """Потокобезопасный общий журнал сообщений между агентами и оператором."""

    def __init__(self, log_file: Optional[str] = None):
        self._messages: List[ChatMessage] = []
        self._lock = threading.Lock()
        self._log_file = log_file

    def append(self, speaker: str, content: str, is_event: bool = False) -> ChatMessage:
        msg = ChatMessage(speaker=speaker, content=content, is_event=is_event)
        with self._lock:
            self._messages.append(msg)
            if self._log_file:
                self._dump_to_file_locked()
        return msg

    def since(self, index: int) -> List[ChatMessage]:
        """Возвращает все сообщения начиная с индекса `index` (инкрементальная обработка)."""
        with self._lock:
            return list(self._messages[index:])

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)

    def build_context(self, agent_name: str, system_prompt: str,
                       history_limit: int = 40) -> List[Dict[str, str]]:
        """Строит список messages для API конкретного агента из последних N сообщений."""
        with self._lock:
            tail = self._messages[-history_limit:]

        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for m in tail:
            if m.speaker == agent_name:
                messages.append({"role": "assistant", "content": m.content})
            else:
                prefix = "[СОБЫТИЕ]" if m.is_event else f"[{m.speaker}]"
                messages.append({"role": "user", "content": f"{prefix} {m.content}"})
        return messages

    def _dump_to_file_locked(self) -> None:
        # вызывается уже под self._lock
        try:
            with open(self._log_file, "w", encoding="utf-8") as f:
                json.dump([m.to_dict() for m in self._messages], f, ensure_ascii=False, indent=2)
        except OSError:
            pass

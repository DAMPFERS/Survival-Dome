"""
LLM-агент: обёртка над DeepSeek Chat Completions API + Function Calling.

Вызовы инструментов и их сырые результаты НЕ попадают в общий чат — другие
агенты видят только финальный текстовый ответ (компактный чат, как решили в ТЗ).
Внутри respond() агент может сделать несколько итераций вызова инструментов
(например: получить get_battery, потом get_channels, потом toggle_channel),
но наружу в SharedChatLog уходит только итоговая реплика.
"""

import json
import logging
from typing import Any, Dict, List

from openai import OpenAI

from chat_bus import ChatMessage, SharedChatLog
from config import AgentConfig
from tool_registry import ToolExecutor, get_tool_schemas

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 6


class Agent:
    def __init__(self, config: AgentConfig, llm_client: OpenAI, model: str,
                 tool_executor: ToolExecutor, bus: SharedChatLog):
        self.config = config
        self._llm = llm_client
        self._model = model
        self._tools = tool_executor
        self._tool_schemas = get_tool_schemas(config.allowed_tools)
        self._bus = bus
        self.last_spoken_ts: float = 0.0

    def respond(self, cause: ChatMessage | None = None) -> str:
        """
        Строит контекст из общего чата, при необходимости вызывает инструменты
        (локально, не публикуя это в общий чат), и добавляет финальный
        текстовый ответ в SharedChatLog.
        """
        messages: List[Dict[str, Any]] = self._bus.build_context(
            self.config.name, self.config.system_prompt
        )

        final_text = "Нет ответа"
        for _ in range(MAX_TOOL_ITERATIONS):
            completion = self._llm.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=self._tool_schemas or None,
            )
            choice = completion.choices[0].message

            if not choice.tool_calls:
                final_text = choice.content or "Нет ответа"
                break

            # Ответ модели с tool_calls добавляем ТОЛЬКО в локальный контекст этого вызова
            messages.append({
                "role": "assistant",
                "content": choice.content or "",
                "tool_calls": [tc.model_dump() for tc in choice.tool_calls],
            })

            for tool_call in choice.tool_calls:
                name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                logger.info("[%s] вызов инструмента %s(%s)", self.config.name, name, args)
                result = self._tools.execute(name, args)
                logger.info("[%s] результат %s: %s", self.config.name, name, result)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
        else:
            final_text = "Превышен лимит вызовов инструментов, не удалось получить финальный ответ"

        self._bus.append(
            self.config.name,
            final_text,
            root_id=cause.root_id if cause else None,
            depth=(cause.depth + 1) if cause else 0,
        )
        return final_text

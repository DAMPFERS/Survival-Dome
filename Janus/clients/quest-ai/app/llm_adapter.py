import re
import httpx
import logging
from abc import ABC, abstractmethod
from app.config import settings

logger = logging.getLogger(__name__)

# Остатки рассуждений, которые некоторые модели оставляют прямо в content
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _clean_content(content: str | None) -> str:
    if not content:
        return ""
    content = _THINK_BLOCK.sub("", content)
    # незакрытый блок или только закрывающий тег — берём текст после последнего </think>
    if "</think>" in content.lower():
        content = re.split(r"</think>", content, flags=re.IGNORECASE)[-1]
    return content.strip()


class LLMAdapter(ABC):
    """Общий интерфейс: любой провайдер должен уметь chat(messages) -> str."""

    @abstractmethod
    async def chat(self, messages: list[dict]) -> str:
        ...

    @abstractmethod
    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict]
    ) -> tuple[str | None, list[dict] | None]:
        """
        Запрос к LLM с поддержкой вызова инструментов (function calling).

        Returns:
            (text_response, tool_calls)
            - text_response: текстовый ответ модели (может быть None если есть tool_calls)
            - tool_calls: список вызовов инструментов (None если модель просто ответила текстом)
        """
        ...


class OpenAICompatibleAdapter(LLMAdapter):
    """Провайдеры с API в формате OpenAI /chat/completions (DeepSeek, Mistral)."""

    name = "LLM"
    api_key = ""
    base_url = ""
    model = ""

    def extra_payload(self, with_tools: bool) -> dict:
        """Параметры, специфичные для провайдера."""
        return {}

    async def _complete(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": settings.LLM_TEMPERATURE,
            "max_tokens": settings.LLM_MAX_TOKENS,
        }
        if tools:
            payload["tools"] = tools
        payload.update(self.extra_payload(with_tools=bool(tools)))

        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        finish_reason = choice.get("finish_reason")
        usage = data.get("usage") or {}
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        logger.info(
            f"{self.name}: finish_reason={finish_reason}, prompt_tokens={usage.get('prompt_tokens')}, "
            f"completion_tokens={usage.get('completion_tokens')}, reasoning_tokens={reasoning_tokens}"
        )
        if finish_reason == "length":
            logger.warning(
                f"{self.name}: ответ обрезан по лимиту LLM_MAX_TOKENS={settings.LLM_MAX_TOKENS} — "
                f"увеличьте лимит или отключите рассуждения (LLM_THINKING=false)"
            )
        return choice["message"]

    async def chat(self, messages: list[dict]) -> str:
        message = await self._complete(messages)
        return _clean_content(message.get("content"))

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict]
    ) -> tuple[str | None, list[dict] | None]:
        message = await self._complete(messages, tools)

        # Проверяем наличие tool_calls
        tool_calls = message.get("tool_calls")
        content = _clean_content(message.get("content"))

        if tool_calls:
            logger.info(f"{self.name} returned {len(tool_calls)} tool call(s)")
            # Преобразуем в унифицированный формат
            parsed_calls = []
            for call in tool_calls:
                parsed_calls.append({
                    "id": call.get("id"),
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"]
                })
            return content, parsed_calls

        return content, None


class DeepSeekAdapter(OpenAICompatibleAdapter):
    name = "DeepSeek"

    def __init__(self):
        self.api_key = settings.DEEPSEEK_API_KEY
        self.base_url = settings.DEEPSEEK_BASE_URL
        self.model = settings.DEEPSEEK_MODEL

    def extra_payload(self, with_tools: bool) -> dict:
        # V4 по умолчанию рассуждает; рассуждения расходуют тот же лимит max_tokens
        return {"thinking": {"type": "enabled" if settings.LLM_THINKING else "disabled"}}


class MistralAdapter(OpenAICompatibleAdapter):
    name = "Mistral"

    def __init__(self):
        self.api_key = settings.MISTRAL_API_KEY
        self.base_url = settings.MISTRAL_BASE_URL
        self.model = settings.MISTRAL_MODEL

    def extra_payload(self, with_tools: bool) -> dict:
        # Mistral требует явного указания tool_choice
        return {"tool_choice": "auto"} if with_tools else {}


def get_llm_adapter() -> LLMAdapter:
    if settings.LLM_PROVIDER == "mistral":
        return MistralAdapter()
    if settings.LLM_PROVIDER == "deepseek":
        return DeepSeekAdapter()
    raise ValueError(f"Неизвестный LLM_PROVIDER: {settings.LLM_PROVIDER}")

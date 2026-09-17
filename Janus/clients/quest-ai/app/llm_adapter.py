import httpx
import logging
from abc import ABC, abstractmethod
from typing import Any
from app.config import settings

logger = logging.getLogger(__name__)


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


class DeepSeekAdapter(LLMAdapter):
    def __init__(self):
        self.api_key = settings.DEEPSEEK_API_KEY
        self.base_url = settings.DEEPSEEK_BASE_URL
        self.model = settings.DEEPSEEK_MODEL

    async def chat(self, messages: list[dict]) -> str:
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
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def chat_with_tools(
        self, 
        messages: list[dict], 
        tools: list[dict]
    ) -> tuple[str | None, list[dict] | None]:
        """DeepSeek поддерживает function calling через параметр tools."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "temperature": settings.LLM_TEMPERATURE,
            "max_tokens": settings.LLM_MAX_TOKENS,
        }
        
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
            resp.raise_for_status()
            data = resp.json()
            
            message = data["choices"][0]["message"]
            
            # Проверяем наличие tool_calls
            tool_calls = message.get("tool_calls")
            content = message.get("content")
            
            if tool_calls:
                logger.info(f"DeepSeek returned {len(tool_calls)} tool call(s)")
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


class MistralAdapter(LLMAdapter):
    def __init__(self):
        self.api_key = settings.MISTRAL_API_KEY
        self.base_url = settings.MISTRAL_BASE_URL
        self.model = settings.MISTRAL_MODEL

    async def chat(self, messages: list[dict]) -> str:
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
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def chat_with_tools(
        self, 
        messages: list[dict], 
        tools: list[dict]
    ) -> tuple[str | None, list[dict] | None]:
        """Mistral поддерживает function calling через параметр tools."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",  # Mistral требует явного указания
            "temperature": settings.LLM_TEMPERATURE,
            "max_tokens": settings.LLM_MAX_TOKENS,
        }
        
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
            resp.raise_for_status()
            data = resp.json()
            
            message = data["choices"][0]["message"]
            
            # Проверяем наличие tool_calls
            tool_calls = message.get("tool_calls")
            content = message.get("content")
            
            if tool_calls:
                logger.info(f"Mistral returned {len(tool_calls)} tool call(s)")
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


def get_llm_adapter() -> LLMAdapter:
    if settings.LLM_PROVIDER == "mistral":
        return MistralAdapter()
    if settings.LLM_PROVIDER == "deepseek":
        return DeepSeekAdapter()
    raise ValueError(f"Неизвестный LLM_PROVIDER: {settings.LLM_PROVIDER}")

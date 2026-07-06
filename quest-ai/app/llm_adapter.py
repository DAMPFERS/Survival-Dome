import httpx
from abc import ABC, abstractmethod
from app.config import settings


class LLMAdapter(ABC):
    """Общий интерфейс: любой провайдер должен уметь chat(messages) -> str."""

    @abstractmethod
    async def chat(self, messages: list[dict]) -> str:
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


def get_llm_adapter() -> LLMAdapter:
    if settings.LLM_PROVIDER == "mistral":
        return MistralAdapter()
    if settings.LLM_PROVIDER == "deepseek":
        return DeepSeekAdapter()
    raise ValueError(f"Неизвестный LLM_PROVIDER: {settings.LLM_PROVIDER}")

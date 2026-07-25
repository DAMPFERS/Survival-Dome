"""
Универсальный клиент для работы с LLM через OpenAI-совместимый API.
Поддерживает DeepSeek, Mistral, OpenAI, Anthropic (через совместимый endpoint).
"""

import os
import json
from pathlib import Path
from datetime import datetime

from openai import OpenAI
from dotenv import load_dotenv
from rich.console import Console

console = Console()

# Загружаем переменные из .env файла
load_dotenv()

# Папка для сырых логов API-ответов (для отладки пустых/битых ответов)
API_LOG_DIR = Path("output") / "api_logs"
API_LOG_DIR.mkdir(parents=True, exist_ok=True)


class LLMClient:
    """
    Обёртка над OpenAI SDK, которая автоматически настраивает
    base_url и модель в зависимости от выбранного провайдера.
    """

    def __init__(self):
        self.provider = os.getenv("LLM_PROVIDER", "deepseek")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")

        if not self.api_key:
            console.print(
                "[bold red]❌ Ошибка:[/bold red] API-ключ не найден!\n"
                "Скопируйте .env.example → .env и вставьте свой ключ.",
            )
            raise SystemExit(1)

        # Инициализируем клиент OpenAI SDK с кастомным base_url
        # Это работает для ВСЕХ провайдеров, т.к. они совместимы с OpenAI API
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        console.print(
            f"[dim]🔌 Провайдер: {self.provider} | "
            f"Модель: {self.model}[/dim]"
        )

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 8192,
        reasoning_effort: str | None = None,
    ) -> str:
        """
        Отправляет запрос к LLM и возвращает текстовый ответ.

        Args:
            system_prompt: Системная инструкция (задаёт роль и формат ответа).
            user_prompt: Промт пользователя (описание схемы/платы).
            temperature: Креативность (0.0–1.0). Низкая = точный код.
            max_tokens: Лимит генерации. ВАЖНО: у reasoning-моделей
                (DeepSeek V4 Pro и т.п.) reasoning-токены и финальный
                content делят этот же бюджет — для больших задач
                (полная схема/плата) нужно значительно больше, чем
                для коротких (уточняющие вопросы).
            reasoning_effort: "low" / "medium" / "high" / "max" / "xhigh" —
                управление глубиной размышлений модели, если провайдер это
                поддерживает (у DeepSeek нет режима "без размышлений" —
                минимальный доступный уровень "low"). None — не передавать
                параметр (поведение по умолчанию у провайдера). Для генерации
                большого детерминированного файла берите "low", чтобы
                оставить максимум бюджета под сам content.

        Returns:
            Строка с ответом от LLM (S-expression KiCad или Python-код).
        """
        try:
            extra_body = {}
            if reasoning_effort is not None:
                extra_body["reasoning_effort"] = reasoning_effort

            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=extra_body or None,
            )

            choice = response.choices[0]
            message = choice.message
            finish_reason = choice.finish_reason

            # content может быть None (не только пустой строкой) —
            # у reasoning-моделей (deepseek-reasoner и т.п.) основной
            # текст может уйти в отдельное поле reasoning_content,
            # а content остаться пустым/None.
            content = message.content or ""
            reasoning_content = getattr(message, "reasoning_content", None)

            usage = getattr(response, "usage", None)
            usage_dict = usage.model_dump() if usage else None

            # Всегда сохраняем сырой ответ на диск — это единственный
            # надёжный способ понять причину пустого/битого ответа
            # постфактум, не гадая по логам консоли.
            log_path = self._log_raw_response(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                content=content,
                reasoning_content=reasoning_content,
                finish_reason=finish_reason,
                usage_dict=usage_dict,
            )

            if not content.strip():
                console.print(
                    f"[bold yellow]⚠️ LLM вернул пустой content.[/bold yellow]\n"
                    f"[dim]finish_reason: {finish_reason} | "
                    f"usage: {usage_dict}[/dim]\n"
                    f"[dim]Полный сырой ответ сохранён: {log_path}[/dim]"
                )
                if reasoning_content:
                    console.print(
                        "[dim]Обнаружен reasoning_content — похоже, используется "
                        "reasoning-модель, у которой ответ ушёл в 'размышления', "
                        "а не в финальный content. Проверьте LLM_MODEL в .env "
                        "(нужна chat-модель, не reasoning) или увеличьте max_tokens.[/dim]"
                    )
                if finish_reason == "length":
                    console.print(
                        "[dim]finish_reason == 'length' — ответ обрезан по лимиту "
                        "max_tokens. Увеличьте max_tokens в llm_client.py.[/dim]"
                    )

            return content.strip()

        except Exception as e:
            error_msg = str(e)
            console.print(f"[bold red]❌ Ошибка API:[/bold red] {error_msg}")
            
            # Подсказка для типичных ошибок
            if "model names are" in error_msg and "but you passed" in error_msg:
                console.print(
                    "\n[bold yellow]💡 Подсказка:[/bold yellow] "
                    "Провайдер обновил список моделей.\n"
                    "Откройте .env и обновите LLM_MODEL на актуальное имя "
                    "из сообщения об ошибке выше."
                )
            elif "401" in error_msg or "Unauthorized" in error_msg:
                console.print(
                    "\n[bold yellow]💡 Подсказка:[/bold yellow] "
                    "Проверьте API-ключ в .env — он недействителен или истёк."
                )
            elif "429" in error_msg or "rate" in error_msg.lower():
                console.print(
                    "\n[bold yellow]💡 Подсказка:[/bold yellow] "
                    "Превышен лимит запросов. Подождите минуту или пополните баланс."
                )
            raise

    def _log_raw_response(
        self,
        system_prompt: str,
        user_prompt: str,
        content: str,
        reasoning_content: str | None,
        finish_reason: str | None,
        usage_dict: dict | None,
    ) -> Path:
        """
        Сохраняет диагностическую информацию о запросе/ответе в JSON-файл.
        Нужно, чтобы при пустом/битом ответе можно было точно понять причину
        постфактум, не полагаясь на вывод в консоль.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_path = API_LOG_DIR / f"api_call_{timestamp}.json"

        log_data = {
            "timestamp": timestamp,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "finish_reason": finish_reason,
            "usage": usage_dict,
            "content_length": len(content),
            "reasoning_content_present": bool(reasoning_content),
            "system_prompt_preview": system_prompt[:300],
            "user_prompt": user_prompt,
            "content": content,
            "reasoning_content": reasoning_content,
        }

        log_path.write_text(
            json.dumps(log_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return log_path
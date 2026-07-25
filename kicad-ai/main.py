"""
KiCad AI Generator — терминальное приложение для генерации
схем и печатных плат через LLM API (DeepSeek / Mistral / OpenAI).

Запуск: python main.py
"""

import os
import re
import json
import uuid
from pathlib import Path
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.spinner import Spinner
from rich.live import Live

from llm_client import LLMClient
from prompts import (
    get_pcb_system_prompt,
    get_clarification_prompt,
    get_netlist_system_prompt,
)
from schematic_builder import build_schematic, schematic_to_kicad9_text, NetlistError

console = Console()

# Папка для сохранения результатов
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)


def extract_json_object(text: str) -> dict:
    """
    Извлекает JSON-объект из ответа LLM, даже если он обёрнут в markdown
    (```json ... ```) или содержит текст до/после самого объекта.

    Raises:
        ValueError: если валидный JSON-объект не найден.
    """
    text = text.strip()
    text = re.sub(r"^```[\w]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("В ответе LLM не найдены фигурные скобки JSON-объекта.")

    candidate = text[start:end + 1]
    return json.loads(candidate)  # может выбросить json.JSONDecodeError


def clean_llm_response(text: str) -> str:
    """
    Очищает ответ LLM от markdown-обёрток и лишнего текста.
    Пытается извлечь KiCad S-expression из любого формата ответа.
    """
    original_text = text
    text = text.strip()
    
    # Попытка 1: Убираем markdown-обёртки (```lisp, ```scheme, ```kicad, и т.д.)
    text = re.sub(r"^```[\w]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    
    # Попытка 2: Ищем начало KiCad S-expression
    # Ищем (kicad_sch или (kicad_pcb в любом месте текста
    sch_match = re.search(r"\(kicad_sch\s", text)
    pcb_match = re.search(r"\(kicad_pcb\s", text)
    
    if sch_match:
        text = text[sch_match.start():]
    elif pcb_match:
        text = text[pcb_match.start():]
    else:
        # Попытка 3: Если LLM сгенерировала Python-скрипт для KiCad
        # Ищем импорты kicad или функции создания схемы
        if "import kicad" in text.lower() or "kicad_pcb" in text.lower():
            console.print(
                "[yellow]⚠️ LLM сгенерировала Python-скрипт вместо S-expression.[/yellow]\n"
                "[dim]Это означает, что модель не поняла задачу. "
                "Попробуйте переключиться на другую модель в .env.[/dim]"
            )
            return ""
        
        # Попытка 4: Если ответ содержит только текст (описание схемы)
        if len(text) < 500 and "(" not in text[:100]:
            console.print(
                "[yellow]⚠️ LLM сгенерировала текстовое описание вместо кода.[/yellow]\n"
                "[dim]Возможно, промпт был недостаточно ясным.[/dim]"
            )
            return ""
    
    # Финальная очистка: убираем лишние пробелы в конце
    text = text.strip()
    
    # Проверяем, что ответ начинается с правильной скобки
    if not (text.startswith("(kicad_sch") or text.startswith("(kicad_pcb")):
        console.print(
            f"[red]❌ Не удалось извлечь валидный KiCad-файл.[/red]\n"
            f"[dim]Первые 200 символов ответа: {text[:200]}...[/dim]"
        )
        return ""
    
    return text


def save_file(content: str, prefix: str, extension: str) -> Path:
    """
    Сохраняет сгенерированный файл в папку output/ с уникальным именем.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = uuid.uuid4().hex[:6]
    filename = f"{prefix}_{timestamp}_{short_id}{extension}"
    filepath = OUTPUT_DIR / filename
    filepath.write_text(content, encoding="utf-8")
    return filepath


def interactive_clarification(llm: LLMClient, description: str) -> str:
    """
    Интерактивный этап: LLM задаёт уточняющие вопросы,
    пользователь отвечает, ответы добавляются к промту.
    """
    console.print(
        Panel(
            "🔍 Анализирую ваш запрос и готовлю уточняющие вопросы...",
            style="cyan",
        )
    )

    # Получаем вопросы от LLM
    questions = llm.generate(
        system_prompt=get_clarification_prompt(),
        user_prompt=description,
        temperature=0.5,
    )

    console.print()
    console.print(Panel(questions, title="Уточняющие вопросы", border_style="yellow"))
    console.print()

    # Спрашиваем, хочет ли пользователь отвечать
    if not Confirm.ask(
        "[bold]Хотите ответить на уточняющие вопросы?[/bold] "
        "(если нет — сгенерирую на основе исходного описания)",
        default=True,
    ):
        return description

    # Собираем ответы
    answers = []
    for line in questions.strip().split("\n"):
        line = line.strip()
        if not line or not any(line.startswith(c) for c in "123456789-•"):
            continue
        # Извлекаем текст вопроса (убираем номер)
        question_text = re.sub(r"^[\d\.\-\•\)]+\s*", "", line)
        if not question_text:
            continue

        answer = Prompt.ask(f"  [cyan]→[/cyan] {question_text}")
        if answer.strip():
            answers.append(f"{question_text}: {answer}")

    if answers:
        enhanced = (
            f"{description}\n\n"
            f"Дополнительные уточнения от пользователя:\n"
            + "\n".join(f"- {a}" for a in answers)
        )
        return enhanced

    return description


def generate_schematic(llm: LLMClient, full_prompt: str) -> Path | None:
    """
    Генерирует файл схемы .kicad_sch.

    Новая архитектура (вместо прямой генерации S-expression):
      1. LLM возвращает структурированный JSON (компоненты + цепи) —
         простая задача, где модель почти не ошибается.
      2. Детерминированный Python-код (schematic_builder.py) собирает
         из этого JSON гарантированно валидный .kicad_sch — вся геометрия
         (координаты пинов, провода, метки) считается кодом, а не LLM.
    """
    console.print()
    with Live(
        Spinner("dots", text="[bold green]Проектирую схему (netlist)...[/bold green]"),
        console=console,
        refresh_per_second=10,
    ):
        raw_response = llm.generate(
            system_prompt=get_netlist_system_prompt(),
            user_prompt=full_prompt,
            temperature=0.2,
            max_tokens=8000,
            reasoning_effort="medium",
        )

    # 1. Парсим JSON-netlist от LLM
    try:
        netlist = extract_json_object(raw_response)
    except (ValueError, json.JSONDecodeError) as e:
        console.print(f"[bold red]❌ LLM вернул невалидный JSON:[/bold red] {e}")
        console.print("[dim]Сырой ответ сохранён для отладки.[/dim]")
        return save_file(raw_response, "debug_sch_netlist", ".txt")

    components = netlist.get("components", [])
    nets = netlist.get("nets", [])

    # 2. Детерминированно собираем схему (без участия LLM)
    try:
        schematic = build_schematic(components, nets)
    except NetlistError as e:
        console.print(f"[bold red]❌ Ошибка в netlist от LLM:[/bold red] {e}")
        console.print(
            "[dim]Сам netlist сохранён для отладки — видно, что именно "
            "предложила модель.[/dim]"
        )
        return save_file(
            json.dumps(netlist, ensure_ascii=False, indent=2),
            "debug_sch_netlist",
            ".json",
        )

    # 3. Сериализуем в файл, дополняя под формат KiCad 9
    content = schematic_to_kicad9_text(schematic)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = uuid.uuid4().hex[:6]
    filepath = OUTPUT_DIR / f"schematic_{timestamp}_{short_id}.kicad_sch"
    filepath.write_text(content, encoding="utf-8")

    # Также сохраняем сам netlist рядом — полезно для отладки/повторной сборки
    netlist_path = filepath.with_suffix(".netlist.json")
    netlist_path.write_text(
        json.dumps(netlist, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return filepath


def generate_pcb(llm: LLMClient, full_prompt: str) -> Path | None:
    """Генерирует файл платы .kicad_pcb."""
    console.print()
    with Live(
        Spinner("dots", text="[bold green]Генерирую плату...[/bold green]"),
        console=console,
        refresh_per_second=10,
    ):
        raw_response = llm.generate(
            system_prompt=get_pcb_system_prompt(),
            user_prompt=full_prompt,
            temperature=0.2,
            max_tokens=32000,
            reasoning_effort="low",
        )

    cleaned = clean_llm_response(raw_response)

    if not cleaned.startswith("(kicad_pcb"):
        console.print(
            "[bold yellow]⚠️ Ответ не начинается с (kicad_pcb. "
            "Попытаюсь извлечь...[/bold yellow]"
        )
        if "(kicad_pcb" not in cleaned:
            console.print("[bold red]❌ Не удалось извлечь плату из ответа.[/bold red]")
            console.print(
                "[dim]Сырой ответ сохранён для отладки. Если он пуст — "
                "смотрите output/api_logs/ (там finish_reason и usage "
                "последнего запроса).[/dim]"
            )
            return save_file(raw_response, "debug_pcb", ".txt")

    filepath = save_file(cleaned, "board", ".kicad_pcb")
    return filepath


def main():
    """Главный цикл интерактивного приложения."""
    console.print(
        Panel(
            "[bold]⚡ KiCad AI Generator[/bold]\n"
            "Генерация схем и печатных плат через LLM API\n"
            "[dim]DeepSeek / Mistral / OpenAI / Anthropic[/dim]",
            border_style="blue",
            expand=False,
        )
    )

    # Инициализируем LLM-клиент
    try:
        llm = LLMClient()
    except SystemExit:
        return

    console.print()

    # === Главный цикл ===
    while True:
        console.rule("[bold]Новый проект[/bold]")
        console.print()

        # 1. Получаем описание проекта
        description = Prompt.ask(
            "[bold cyan]📝 Опишите ваш проект[/bold cyan]\n"
            "[dim](например: 'Схема линейного стабилизатора на LM317, "
            "вход 12В, выход 5В, ток до 1А')\n"
            "[dim]Для выхода введите: quit[/dim]"
        )

        if description.strip().lower() in ("quit", "exit", "q", "выход"):
            console.print("\n[bold green]👋 До свидания![/bold green]")
            break

        if not description.strip():
            console.print("[yellow]⚠️ Пустое описание, попробуйте ещё раз.[/yellow]")
            continue

        # 2. Выбор типа генерации
        console.print()
        gen_type = Prompt.ask(
            "[bold]Что сгенерировать?[/bold]",
            choices=["1", "2", "3"],
            default="1",
        )
        # 1 = схема, 2 = плата, 3 = оба

        type_labels = {"1": "📐 Схема", "2": "🟢 Плата", "3": "📐 Схема + 🟢 Плата"}
        console.print(f"[dim]Выбрано: {type_labels[gen_type]}[/dim]")

        # 3. Интерактивное уточнение
        console.print()
        full_prompt = interactive_clarification(llm, description)

        # 4. Генерация
        results = []

        if gen_type in ("1", "3"):
            path = generate_schematic(llm, full_prompt)
            if path:
                results.append(("Схема", path))

        if gen_type in ("2", "3"):
            path = generate_pcb(llm, full_prompt)
            if path:
                results.append(("Плата", path))

        # 5. Вывод результатов
        console.print()
        if results:
            console.print(
                Panel(
                    "\n".join(
                        f"  ✅ [bold]{name}:[/bold] [link=file://{p.absolute()}]"
                        f"{p.name}[/link]"
                        for name, p in results
                    ),
                    title="🎉 Готово!",
                    border_style="green",
                )
            )
            console.print(
                f"\n[dim]📂 Все файлы в папке: {OUTPUT_DIR.absolute()}[/dim]"
            )
            console.print(
                "[dim]💡 Откройте файл в KiCad: "
                "File → Open → выберите сгенерированный файл[/dim]"
            )
        else:
            console.print(
                "[bold red]❌ Не удалось сгенерировать файлы. "
                "Попробуйте упростить описание.[/bold red]"
            )

        # 6. Продолжить?
        console.print()
        if not Confirm.ask("[bold]Сгенерировать ещё один проект?[/bold]", default=True):
            console.print("\n[bold green]👋 До свидания![/bold green]")
            break


if __name__ == "__main__":
    main()
import zipfile
import io
from pathlib import Path
from typing import List, Dict, Any

import httpx
import questionary
from rich.console import Console
from rich.tree import Tree
from rich.progress import Progress, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
from rich.panel import Panel
from rich import print as rprint

# ====================== НАСТРОЙКИ ======================
SERVER_URL = "http://127.0.0.1:8765"          
DOWNLOAD_DIR = Path("downloads")              # куда сохранять архив и распаковывать


console = Console()


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def fetch_tree() -> Dict[str, Any]:
    """Получает дерево файловой системы с сервера"""
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{SERVER_URL}/fs")
        resp.raise_for_status()
        return resp.json()


def build_rich_tree(node: Dict[str, Any], tree: Tree | None = None) -> Tree:
    """Строит красивое дерево для отображения"""
    if tree is None:
        tree = Tree(f"[bold cyan]{node['name']}[/]")

    for child in node.get("children", []):
        if child["type"] == "directory":
            branch = tree.add(f"[bold blue]📁 {child['name']}[/]")
            build_rich_tree(child, branch)
        else:
            size_mb = child.get("size", 0) / (1024 * 1024)
            tree.add(f"[green]📄 {child['name']}[/] [dim]({size_mb:.2f} MB)[/]")

    return tree


def flatten_tree(node: Dict[str, Any], prefix: str = "", depth: int = 0) -> List[Dict[str, str]]:
    """Плоский список с отступами для красивого отображения"""
    items = []

    current_path = f"{prefix}/{node['name']}".strip("/") if node["name"] != "." else prefix

    if node["name"] != ".":
        display = f"{'  ' * depth}📁 {node['name']}/"
        items.append({
            "name": display,
            "value": current_path,
            "is_dir": True
        })

    for child in node.get("children", []):
        if child["type"] == "directory":
            new_prefix = f"{current_path}/{child['name']}".strip("/") if current_path else child["name"]
            items.extend(flatten_tree(child, current_path, depth + 1))
        else:
            display = f"{'  ' * (depth + 1)}📄 {child['name']}"
            rel_path = f"{current_path}/{child['name']}".strip("/") if current_path else child["name"]
            items.append({
                "name": display,
                "value": rel_path,
                "is_dir": False
            })

    return items


def get_all_children_paths(tree_data: Dict[str, Any], target_path: str) -> List[str]:
    """Возвращает все пути (файлы + папки) внутри указанной директории"""
    result = []

    def walk(node: Dict[str, Any], current: str = ""):
        curr_path = f"{current}/{node['name']}".strip("/") if node["name"] != "." else current

        if curr_path == target_path or curr_path.startswith(target_path + "/"):
            if node["name"] != ".":
                result.append(curr_path)

        for child in node.get("children", []):
            if child["type"] == "directory":
                walk(child, curr_path)
            else:
                file_path = f"{curr_path}/{child['name']}".strip("/")
                if file_path.startswith(target_path + "/") or curr_path == target_path:
                    result.append(file_path)

    walk(tree_data)
    return result


def select_items(tree_data: Dict[str, Any]) -> List[str]:
    """Интерактивный выбор + автоматическое расширение выбранных папок"""
    flat_items = flatten_tree(tree_data)

    if not flat_items:
        console.print("[red]Нет доступных файлов и папок[/]")
        return []

    choices = [
        questionary.Choice(title=item["name"], value=item["value"])
        for item in flat_items
    ]

    selected = questionary.checkbox(
        "Выберите файлы и папки (Пробел — выбрать, Enter — подтвердить):",
        choices=choices,
        instruction="(↑/↓ навигация, Пробел — выбор, a — всё, Enter — далее)"
    ).ask()

    if not selected:
        return []

    # Расширяем выбранные папки: добавляем всё их содержимое
    final_selected = set(selected)
    for path in selected:
        # Проверяем, является ли путь папкой
        children = get_all_children_paths(tree_data, path)
        if children:  # значит это папка
            final_selected.update(children)

    return sorted(final_selected)


def download_and_extract(selected_paths: List[str]):
    """Скачивает ZIP и распаковывает с сохранением структуры"""
    if not selected_paths:
        console.print("[yellow]Ничего не выбрано[/]")
        return

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    zip_path = DOWNLOAD_DIR / "download.zip"

    console.print("\n[bold cyan]Отправка запроса на сервер...[/]")

    with httpx.Client(timeout=None) as client:  # без таймаута — большие архивы
        with client.stream(
            "POST",
            f"{SERVER_URL}/download",
            json={"paths": selected_paths}
        ) as response:
            response.raise_for_status()

            total = int(response.headers.get("Content-Length", 0))

            with Progress(
                "[progress.description]{task.description}",
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console
            ) as progress:
                task = progress.add_task("Скачивание...", total=total)

                with open(zip_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=65536):
                        f.write(chunk)
                        progress.update(task, advance=len(chunk))

    console.print(f"\n[green]Архив сохранён:[/] {zip_path}")

    # Распаковка с сохранением структуры
    extract_dir = DOWNLOAD_DIR / "extracted"
    extract_dir.mkdir(exist_ok=True)

    console.print("[bold cyan]Распаковка архива...[/]")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    console.print(Panel.fit(
        f"[bold green]Готово![/]\n"
        f"Файлы распакованы в: [cyan]{extract_dir.resolve()}[/]",
        title="Успех",
        border_style="green"
    ))


# ====================== ГЛАВНАЯ ЛОГИКА ======================
def main():
    console.clear()
    console.print(Panel.fit(
        "[bold]Клиент скачивания файлов[/]\n"
        f"Сервер: [cyan]{SERVER_URL}[/]",
        border_style="blue"
    ))

    try:
        console.print("\n[bold]Получение списка файлов с сервера...[/]")
        tree_data = fetch_tree()

        # Показываем дерево
        rich_tree = build_rich_tree(tree_data)
        console.print("\n[bold]Доступные файлы и папки:[/]")
        console.print(rich_tree)
        console.print()

        # Интерактивный выбор
        selected = select_items(tree_data)
        

        if not selected:
            console.print("[yellow]Выбор отменён[/]")
            return
        
        console.print("\n[bold]Будет скачано:[/]")
        for path in selected:
            # Простая эвристика: если путь заканчивается на / или есть дети — папка
            console.print(f"  • {path}")

        console.print("\n[bold]Вы выбрали:[/]")
        for path in selected:
            console.print(f"  • {path}")

        # Подтверждение
        if not questionary.confirm("Начать скачивание?").ask():
            console.print("[yellow]Отменено пользователем[/]")
            return

        download_and_extract(selected)

    except httpx.ConnectError:
        console.print(f"[bold red]Не удалось подключиться к серверу {SERVER_URL}[/]")
        console.print("Проверьте, что сервер запущен и адрес указан правильно.")
    except httpx.HTTPStatusError as e:
        console.print(f"[bold red]Ошибка сервера:[/] {e.response.status_code} — {e.response.text}")
    except Exception as e:
        console.print(f"[bold red]Неожиданная ошибка:[/] {e}")


if __name__ == "__main__":
    main()
from pathlib import Path
from typing import List
import zipfile
import io
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# ====================== НАСТРОЙКИ ======================
# Корень, который сервер разрешает отдавать
ROOT_DIR = Path(r"D:\PROGRAMS\Survival-Dome\Janus\server").resolve()          

# Можно ограничить максимальный размер архива (в байтах). None = без ограничения
MAX_ARCHIVE_SIZE = None          # например 2 * 1024**3  (2 ГБ)

app = FastAPI(title="Local File Download Server")


# ====================== МОДЕЛИ ======================
class DownloadRequest(BaseModel):
    paths: List[str] = Field(..., description="Список относительных путей (файлы и/или директории)")


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def is_safe_path(path: Path) -> bool:
    """Проверяет, что путь находится внутри ROOT_DIR"""
    try:
        path.resolve().relative_to(ROOT_DIR)
        return True
    except ValueError:
        return False


def build_tree(current: Path) -> dict:
    """Рекурсивно строит дерево директорий и файлов"""
    node = {
        "name": current.name if current != ROOT_DIR else ".",
        "type": "directory",
        "children": []
    }

    try:
        for item in sorted(current.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if not is_safe_path(item):
                continue

            if item.is_dir():
                node["children"].append(build_tree(item))
            else:
                node["children"].append({
                    "name": item.name,
                    "type": "file",
                    "size": item.stat().st_size
                })
    except PermissionError:
        pass

    return node


def collect_files(selected_paths: List[str]) -> List[tuple[Path, str]]:
    """
    Селективная сборка:
    - Файл → только этот файл
    - Папка → вся папка рекурсивно
    """
    files_to_add = []

    for rel_path_str in selected_paths:
        rel_path = Path(rel_path_str)
        abs_path = (ROOT_DIR / rel_path).resolve()

        if not is_safe_path(abs_path):
            raise HTTPException(status_code=400, detail=f"Небезопасный путь: {rel_path_str}")

        if not abs_path.exists():
            raise HTTPException(status_code=404, detail=f"Путь не найден: {rel_path_str}")

        if abs_path.is_file():
            arcname = str(rel_path).replace("\\", "/")
            files_to_add.append((abs_path, arcname))
        else:
            # Папка — добавляем всё содержимое рекурсивно
            for file_path in abs_path.rglob("*"):
                if file_path.is_file() and is_safe_path(file_path):
                    relative = file_path.relative_to(ROOT_DIR)
                    arcname = str(relative).replace("\\", "/")
                    files_to_add.append((file_path, arcname))

    # Убираем дубликаты
    unique = {}
    for abs_p, arc in files_to_add:
        unique[arc] = abs_p

    return [(path, name) for name, path in unique.items()]


# ====================== ЭНДПОИНТЫ ======================
@app.get("/fs")
def get_filesystem_tree():
    """Возвращает дерево файловой системы начиная с ROOT_DIR"""
    if not ROOT_DIR.exists():
        raise HTTPException(status_code=500, detail="Корневая директория не существует")

    return build_tree(ROOT_DIR)


@app.post("/download")
def download_selected(req: DownloadRequest):
    """
    Принимает список путей и отдаёт ZIP-архив
    с полностью сохранённой структурой директорий.
    """
    if not req.paths:
        raise HTTPException(status_code=400, detail="Список путей пуст")

    files = collect_files(req.paths)

    if not files:
        raise HTTPException(status_code=400, detail="Не найдено ни одного файла для скачивания")

    # Создаём ZIP в памяти (для локальной сети это обычно нормально)
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for abs_path, arcname in files:
            zf.write(abs_path, arcname)

    zip_buffer.seek(0)
    size = zip_buffer.getbuffer().nbytes

    if MAX_ARCHIVE_SIZE and size > MAX_ARCHIVE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"Архив слишком большой ({size} байт). Максимум: {MAX_ARCHIVE_SIZE}"
        )

    headers = {
        "Content-Disposition": 'attachment; filename="download.zip"',
        "Content-Length": str(size)
    }

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers=headers
    )


# ====================== ЗАПУСК ======================
if __name__ == "__main__":
    import uvicorn
    print(f"Сервер отдаёт содержимое: {ROOT_DIR}")
    uvicorn.run("server:app", host="0.0.0.0", port=8765, reload=True)
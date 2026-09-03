"""
Индексация материалов лора в векторную базу.

Как организовать файлы:
    lore/
        stage_1/
            introduction.docx
            location_A.pdf
        stage_2/
            location_B.docx
            hint_object_key.pdf
        ...

Номер папки stage_N задаёт этап, на котором материал становится доступен
персонажу. Запуск:
    python ingest.py
"""
import os
import re
from docx import Document
from pypdf import PdfReader

from app.config import settings
from app.rag import get_collection

CHUNK_SIZE = 800  # символов
CHUNK_OVERLAP = 150


def read_docx(path: str) -> str:
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def read_pdf(path: str) -> str:
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start = end - overlap
    return [c.strip() for c in chunks if c.strip()]


def parse_stage_dir_name(dirname: str) -> int | None:
    match = re.match(r"stage_(\d+)", dirname)
    return int(match.group(1)) if match else None


def main():
    collection = get_collection()

    # чистим коллекцию перед повторной индексацией, чтобы не было дублей
    existing = collection.get()
    if existing["ids"]:
        collection.delete(ids=existing["ids"])

    doc_id = 0
    total_chunks = 0

    for dirname in sorted(os.listdir(settings.LORE_DIR)):
        stage = parse_stage_dir_name(dirname)
        if stage is None:
            continue

        stage_path = os.path.join(settings.LORE_DIR, dirname)
        if not os.path.isdir(stage_path):
            continue

        for filename in sorted(os.listdir(stage_path)):
            filepath = os.path.join(stage_path, filename)
            ext = filename.lower().split(".")[-1]

            if ext == "docx":
                text = read_docx(filepath)
            elif ext == "pdf":
                text = read_pdf(filepath)
            else:
                print(f"  пропуск (неподдерживаемый формат): {filename}")
                continue

            chunks = chunk_text(text)
            ids = [f"doc{doc_id}_chunk{i}" for i in range(len(chunks))]
            metadatas = [{"stage": stage, "source": filename} for _ in chunks]

            if chunks:
                collection.add(documents=chunks, ids=ids, metadatas=metadatas)

            print(f"  проиндексировано: {filename} (этап {stage}, чанков: {len(chunks)})")
            doc_id += 1
            total_chunks += len(chunks)

    print(f"\nГотово. Всего чанков в базе: {total_chunks}")


if __name__ == "__main__":
    main()

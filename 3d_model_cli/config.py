# config.py
import os
from pathlib import Path

# Путь для сохранения файлов
OUTPUT_DIR = Path("outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

# Конфигурация API 3D AI Studio
API_BASE_URL = "https://api.3daistudio.com"
API_KEY = os.getenv("API_KEY_3D_AI_STUDIO", "1g60cBlIauWeNIstBBIyvMy2BRzBOM53c9SWC")  # Замени на свой токен

# Поддерживаемые форматы для 3D-моделей
SUPPORTED_FORMATS = ["glb", "obj", "fbx"]
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # --- LLM provider: "deepseek" или "mistral" ---
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "deepseek").lower()

    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    MISTRAL_API_KEY: str = os.getenv("MISTRAL_API_KEY", "")
    MISTRAL_MODEL: str = os.getenv("MISTRAL_MODEL", "mistral-large-latest")
    MISTRAL_BASE_URL: str = "https://api.mistral.ai/v1"

    LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "600"))

    # --- Пути к данным ---
    CHROMA_PATH: str = os.getenv("CHROMA_PATH", "./data/chroma")
    DB_PATH: str = os.getenv("DB_PATH", "./data/game.db")
    LORE_DIR: str = os.getenv("LORE_DIR", "./lore")

    # --- Игра ---
    MAX_STAGE: int = int(os.getenv("MAX_STAGE", "5"))
    HISTORY_WINDOW: int = int(os.getenv("HISTORY_WINDOW", "12"))  # сколько последних сообщений держим в контексте
    TOP_K_CHUNKS: int = int(os.getenv("TOP_K_CHUNKS", "4"))  # сколько кусков лора подтягиваем на вопрос

    # --- Персонаж ---
    CHARACTER_NAME: str = os.getenv("CHARACTER_NAME", "Хранитель Архива")

    # --- Админ-панель ---
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "change_me_please")


settings = Settings()

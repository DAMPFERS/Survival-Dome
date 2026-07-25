import time
import logging
from stt_service import SpeechToTextService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(levelname)s: %(message)s",
)


def on_recognized(text: str) -> None:
    print(f"\n🎙️  [{stt.active_engine}] → {text}\n")


# --- Конфигурация ---
stt = SpeechToTextService(
    # Локальная модель Vosk (основной движок)
    model_path="vosk-model-small-ru-0.22",

    # Callback для получения текста
    callback=on_recognized,

    # Автопоиск микрофона — можно указать часть имени для поиска
    # preferred_mic_name="USB",   # найдёт "USB PnP Sound Device" и т.п.
    # preferred_mic_name="Blue",  # для Blue Yeti и похожих

    # Или задать индекс вручную (см. список при запуске)
    # device=1,

    # Google Cloud фолбэк (включить если нужен)
    use_fallback=False,
    # fallback_credentials="/path/to/google-key.json",
    language="ru-RU",
)

stt.start()
print(f"\n✅ Активный движок: {stt.active_engine}")
print("Говорите в микрофон. Ctrl+C для остановки.\n")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nОстанавливаю...")
finally:
    stt.stop()
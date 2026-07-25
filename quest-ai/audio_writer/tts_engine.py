"""
Надёжный модуль Text-to-Speech с поддержкой двух бэкендов:
- pyttsx3  — быстрый, оффлайн, системные голоса
- edge     — красивые нейросетевые голоса Microsoft (рекомендуется)
"""

import asyncio
import os
import queue
import tempfile
import threading
import time
from typing import Optional, Callable, Literal

import pyttsx3

# ---------- Опциональные зависимости для edge-tts ----------
try:
    import edge_tts
    import pygame
    EDGE_AVAILABLE = True
except ImportError:
    EDGE_AVAILABLE = False


BackendType = Literal["pyttsx3", "edge"]


class TTSEngine:
    def __init__(
        self,
        backend: BackendType = "edge",          # ← по умолчанию красивые голоса
        rate: int = 180,
        volume: float = 1.0,
        voice: Optional[str] = None,            # для edge: "ru-RU-SvetlanaNeural"
        voice_id: Optional[str] = None,         # для pyttsx3
    ):
        if backend == "edge" and not EDGE_AVAILABLE:
            raise ImportError(
                "Для backend='edge' нужно установить:\n"
                "pip install edge-tts pygame"
            )

        self.backend = backend
        self._rate = rate
        self._volume = volume
        self._voice = voice or "ru-RU-SvetlanaNeural"   # красивый русский женский
        self._voice_id = voice_id

        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._is_speaking = False
        self._current_temp_file: Optional[str] = None

        self._local = threading.local()

        # Инициализация pygame один раз (если edge)
        if self.backend == "edge":
            pygame.mixer.init()

        self._worker = threading.Thread(
            target=self._worker_loop,
            name="TTS-Worker",
            daemon=True,
        )
        self._worker.start()

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _get_pyttsx3_engine(self) -> pyttsx3.Engine:
        if not hasattr(self._local, "engine"):
            engine = pyttsx3.init()
            engine.setProperty("rate", self._rate)
            engine.setProperty("volume", self._volume)
            if self._voice_id:
                engine.setProperty("voice", self._voice_id)
            self._local.engine = engine
        return self._local.engine

    async def _edge_speak(self, text: str) -> None:
        """Генерирует речь через edge-tts и воспроизводит."""
        communicate = edge_tts.Communicate(text, self._voice)

        # Создаём временный файл
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            temp_path = f.name

        self._current_temp_file = temp_path

        try:
            await communicate.save(temp_path)

            # Воспроизведение через pygame
            pygame.mixer.music.load(temp_path)
            pygame.mixer.music.set_volume(self._volume)
            pygame.mixer.music.play()

            # Ждём окончания воспроизведения
            while pygame.mixer.music.get_busy():
                if self._stop_event.is_set():
                    pygame.mixer.music.stop()
                    break
                await asyncio.sleep(0.05)

        finally:
            # Удаляем временный файл
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
            self._current_temp_file = None

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                text, callback = self._queue.get(timeout=0.3)
            except queue.Empty:
                continue

            self._is_speaking = True
            try:
                if self.backend == "pyttsx3":
                    engine = self._get_pyttsx3_engine()
                    engine.say(text)
                    engine.runAndWait()
                else:  # edge
                    # Запускаем асинхронный код внутри потока
                    asyncio.run(self._edge_speak(text))

            except Exception as e:
                print(f"[TTS] Ошибка: {e}")
            finally:
                self._is_speaking = False
                self._queue.task_done()
                if callback:
                    try:
                        callback()
                    except Exception as e:
                        print(f"[TTS] Ошибка callback: {e}")

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def speak(
        self,
        text: str,
        *,
        block: bool = False,
        callback: Optional[Callable[[], None]] = None,
    ) -> None:
        if not text or not text.strip():
            return

        self._queue.put((text.strip(), callback))

        if block:
            self._queue.join()

    def stop(self) -> None:
        """Останавливает текущую речь и очищает очередь."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

        if self.backend == "edge":
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
        else:
            try:
                if hasattr(self._local, "engine"):
                    self._local.engine.stop()
            except Exception:
                pass

    def is_speaking(self) -> bool:
        return self._is_speaking or not self._queue.empty()

    def shutdown(self) -> None:
        self.stop()
        self._stop_event.set()
        self._worker.join(timeout=3.0)
        if self.backend == "edge":
            try:
                pygame.mixer.quit()
            except Exception:
                pass

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        if self.backend == "edge":
            try:
                pygame.mixer.music.set_volume(self._volume)
            except Exception:
                pass
        elif hasattr(self._local, "engine"):
            self._local.engine.setProperty("volume", self._volume)

    def set_rate(self, rate: int) -> None:
        """Работает только для pyttsx3. У edge-tts скорость задаётся голосом."""
        self._rate = rate
        if self.backend == "pyttsx3" and hasattr(self._local, "engine"):
            self._local.engine.setProperty("rate", rate)

    # ------------------------------------------------------------------
    # Список голосов
    # ------------------------------------------------------------------

    def list_voices(self) -> list[dict]:
        if self.backend == "pyttsx3":
            engine = pyttsx3.init()
            voices = []
            for v in engine.getProperty("voices"):
                voices.append({
                    "id": v.id,
                    "name": v.name,
                    "languages": getattr(v, "languages", []),
                })
            engine.stop()
            return voices

        # ---------- edge-tts ----------
        async def _get_voices():
            # Правильный способ: сначала await, потом обычный for
            voices = await edge_tts.list_voices()
            return voices

        all_voices = asyncio.run(_get_voices())

        result = []
        for v in all_voices:
            # Оставляем русские + несколько хороших английских
            if v["Locale"].startswith("ru-") or v["ShortName"] in (
                "en-US-JennyNeural",
                "en-US-GuyNeural",
                "en-GB-SoniaNeural",
                "en-GB-RyanNeural",
            ):
                result.append({
                    "id": v["ShortName"],
                    "name": v["FriendlyName"],
                    "gender": v["Gender"],
                    "locale": v["Locale"],
                })
        return result


# ------------------------------------------------------------------
# Пример использования
# ------------------------------------------------------------------
if __name__ == "__main__":
    tts = TTSEngine(backend="edge", voice="ru-RU-SvetlanaNeural", volume=0.9)

    print("=== Доступные красивые голоса (edge) ===")
    voices = tts.list_voices()
    for v in voices:
        print(f"{v['id']:30} | {v['name']} ({v['gender']})")

    print("\nГоворим красивым голосом...")
    tts.speak("Привет! Теперь у меня гораздо более приятный и естественный голос.")

    for i in range(4):
        print(f"Основной поток работает... {i + 1}")
        time.sleep(0.8)

    tts.speak("Можно говорить несколько фраз подряд.", block=True)
    # Вариант A — чисто
    tts.speak("Ну что, приступим.")

    # Вариант B — с паузой и вздохом в интонации
    tts.speak("...Ну что... приступим.")

    # Вариант C — более живой
    tts.speak("Ну что ж... приступим.")
    print("Готово.")

    tts.shutdown()
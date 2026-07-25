import threading
import queue
import json
import time
import numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer
import logging

from mic_selector import find_microphone, verify_microphone
from gcloud_stt import GoogleCloudSTT, GCLOUD_AVAILABLE

logger = logging.getLogger(__name__)


class SpeechToTextService:
    """
    Сервис распознавания речи с:
    - Автоматическим выбором микрофона
    - Основным движком: Vosk (офлайн)
    - Фолбэком: Google Cloud STT (если Vosk недоступен или указан явно)
    """

    SAMPLE_RATE = 16000
    BLOCK_SIZE  = 8000

    # Порог качества: если за N секунд Vosk ничего не распознал —
    # считаем что что-то пошло не так (для будущей логики мониторинга)
    VOSK_TIMEOUT_SEC = 30

    def __init__(
        self,
        model_path: str | None = None,
        callback=None,
        device: int | None = None,
        preferred_mic_name: str | None = None,
        use_fallback: bool = False,
        fallback_credentials: str | None = None,
        language: str = "ru-RU",
    ):
        """
        :param model_path:           Путь к модели Vosk (None = только Google Cloud)
        :param callback:             callback(text: str) для результатов
        :param device:               Индекс микрофона (None = автоопределение)
        :param preferred_mic_name:   Имя микрофона для поиска (например "USB")
        :param use_fallback:         Включить Google Cloud как запасной движок
        :param fallback_credentials: Путь к JSON-ключу Google (None = env var)
        :param language:             Код языка для Google Cloud
        """
        self._model_path    = model_path
        self._callback      = callback
        self._device        = device
        self._pref_mic      = preferred_mic_name
        self._use_fallback  = use_fallback and GCLOUD_AVAILABLE
        self._fallback_cred = fallback_credentials
        self._language      = language

        self.result_queue: queue.Queue[str] = queue.Queue()

        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._stop_event  = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_engine: str = "none"

    # ------------------------------------------------------------------ #
    #  Публичный интерфейс                                                 #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Запустить сервис: выбрать микрофон и запустить поток."""
        if self._thread and self._thread.is_alive():
            logger.warning("STT-сервис уже запущен")
            return

        # --- Автоопределение микрофона ---
        if self._device is None:
            self._device = find_microphone(
                preferred_name=self._pref_mic
            )
            if self._device is None:
                raise RuntimeError("Микрофон не найден. "
                                   "Подключите микрофон и повторите запуск.")

            if not verify_microphone(self._device, self.SAMPLE_RATE):
                raise RuntimeError(
                    f"Микрофон [{self._device}] найден, но не работает на "
                    f"{self.SAMPLE_RATE} Гц. Попробуйте другое устройство."
                )

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="stt-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("STT-сервис запущен (устройство: %s)", self._device)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("STT-сервис остановлен")

    @property
    def active_engine(self) -> str:
        """Имя активного движка: 'vosk', 'google_cloud' или 'none'."""
        return self._active_engine

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ #
    #  Внутренняя логика                                                   #
    # ------------------------------------------------------------------ #

    def _audio_callback(self, indata, frames, time_, status) -> None:
        if status:
            logger.warning("sounddevice: %s", status)
        audio_bytes = (indata * 32767).astype(np.int16).tobytes()
        self._audio_queue.put(audio_bytes)

    def _run(self) -> None:
        """Выбрать движок и запустить цикл распознавания."""

        # Приоритет: Vosk (если задан model_path) → Google Cloud (если включён)
        if self._model_path:
            self._run_vosk()
        elif self._use_fallback:
            logger.info("model_path не задан — сразу используем Google Cloud")
            self._run_google_cloud()
        else:
            logger.error("Не задан ни model_path (Vosk), "
                         "ни use_fallback=True (Google Cloud). Выход.")

    def _run_vosk(self) -> None:
        """Цикл распознавания через Vosk."""
        try:
            logger.info("Загрузка Vosk из '%s'...", self._model_path)
            model      = Model(self._model_path)
            recognizer = KaldiRecognizer(model, self.SAMPLE_RATE)
            self._active_engine = "vosk"
            logger.info("Vosk готов, слушаю микрофон [%d]...", self._device)

            with sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                blocksize=self.BLOCK_SIZE,
                device=self._device,
                channels=1,
                dtype="float32",
                callback=self._audio_callback,
            ):
                while not self._stop_event.is_set():
                    self._process_audio_vosk(recognizer)

        except Exception as e:
            logger.error("Ошибка Vosk: %s", e)

            if self._use_fallback:
                logger.warning("Vosk упал → переключаемся на Google Cloud")
                self._run_google_cloud()
            else:
                logger.error("Фолбэк не включён. STT-сервис остановлен.")

        finally:
            self._active_engine = "none"

    def _process_audio_vosk(self, recognizer: KaldiRecognizer) -> None:
        try:
            audio_bytes = self._audio_queue.get(timeout=0.5)
        except queue.Empty:
            return

        if recognizer.AcceptWaveform(audio_bytes):
            result = json.loads(recognizer.Result())
            text   = result.get("text", "").strip()
            if text:
                self._emit(text)
        else:
            partial = json.loads(recognizer.PartialResult())
            partial_text = partial.get("partial", "").strip()
            if partial_text:
                logger.debug("Vosk (partial): %s", partial_text)

    def _run_google_cloud(self) -> None:
        """Цикл распознавания через Google Cloud STT."""
        if not GCLOUD_AVAILABLE:
            logger.error("google-cloud-speech не установлен. Установите: "
                         "pip install google-cloud-speech")
            return

        self._active_engine = "google_cloud"
        gcloud = GoogleCloudSTT(
            language=self._language,
            credentials_path=self._fallback_cred,
        )
        logger.info("Google Cloud STT активирован")

        # Google Cloud имеет лимит ~5 минут на один стриминг-запрос
        # Перезапускаем цикл автоматически
        with sd.InputStream(
            samplerate=self.SAMPLE_RATE,
            blocksize=self.BLOCK_SIZE,
            device=self._device,
            channels=1,
            dtype="float32",
            callback=self._audio_callback,
        ):
            while not self._stop_event.is_set():
                try:
                    gcloud.recognize_stream(
                        audio_queue=self._audio_queue,
                        callback=self._emit,
                    )
                except Exception as e:
                    logger.error("Google Cloud STT ошибка: %s. "
                                 "Перезапуск через 3 сек...", e)
                    time.sleep(3)

        self._active_engine = "none"

    def _emit(self, text: str) -> None:
        logger.info("[%s] %s", self._active_engine, text)
        if self._callback:
            self._callback(text)
        else:
            self.result_queue.put(text)
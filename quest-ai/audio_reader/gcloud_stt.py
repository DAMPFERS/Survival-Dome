import queue
import threading
import logging
from typing import Generator

logger = logging.getLogger(__name__)

# Флаг доступности — импорт не упадёт если библиотека не установлена
try:
    from google.cloud import speech
    GCLOUD_AVAILABLE = True
except ImportError:
    GCLOUD_AVAILABLE = False
    logger.warning("google-cloud-speech не установлен. "
                   "Google Cloud фолбэк недоступен.")


class GoogleCloudSTT:
    """
    Потоковое распознавание через Google Cloud Speech-to-Text API.

    Требования:
    - pip install google-cloud-speech
    - Переменная окружения: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
      ИЛИ передать путь к ключу в конструктор

    Использование как фолбэка:
        gcloud = GoogleCloudSTT(language="ru-RU")
        gcloud.start(audio_queue, result_callback)
    """

    SAMPLE_RATE = 16000

    def __init__(
        self,
        language: str = "ru-RU",
        credentials_path: str | None = None,
    ):
        """
        :param language:         Код языка (ru-RU, en-US и т.д.)
        :param credentials_path: Путь к JSON-ключу сервисного аккаунта Google.
                                 Если None — используется GOOGLE_APPLICATION_CREDENTIALS
        """
        if not GCLOUD_AVAILABLE:
            raise RuntimeError(
                "Установите google-cloud-speech: pip install google-cloud-speech"
            )

        self._language         = language
        self._credentials_path = credentials_path
        self._client           = None
        self._stop_event       = threading.Event()

    def _get_client(self):
        """Создать клиент Google Cloud (с поддержкой кастомного ключа)."""
        if self._credentials_path:
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                self._credentials_path
            )
            return speech.SpeechClient(credentials=credentials)
        return speech.SpeechClient()  # из переменной окружения

    def _audio_generator(
        self, audio_queue: queue.Queue
    ) -> Generator[bytes, None, None]:
        """
        Генератор аудио-чанков для стримингового запроса к Google.
        Читает из очереди пока не получит сигнал остановки.
        """
        while not self._stop_event.is_set():
            try:
                chunk = audio_queue.get(timeout=0.5)
                yield chunk
            except queue.Empty:
                continue

    def recognize_stream(
        self,
        audio_queue: queue.Queue,
        callback,
        interim_results: bool = False,
    ) -> None:
        """
        Запустить потоковое распознавание (блокирующий метод — вызывать в потоке).

        :param audio_queue:     Очередь с аудио-байтами (int16 PCM)
        :param callback:        Функция callback(text: str) для результатов
        :param interim_results: Передавать промежуточные результаты
        """
        self._stop_event.clear()
        client = self._get_client()

        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self.SAMPLE_RATE,
            language_code=self._language,
            enable_automatic_punctuation=True,
        )
        streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=interim_results,
        )

        requests = (
            speech.StreamingRecognizeRequest(audio_content=chunk)
            for chunk in self._audio_generator(audio_queue)
        )

        logger.info("Google Cloud STT: начало стриминга")
        try:
            responses = client.streaming_recognize(streaming_config, requests)
            for response in responses:
                if self._stop_event.is_set():
                    break
                for result in response.results:
                    if result.is_final:
                        text = result.alternatives[0].transcript.strip()
                        if text:
                            logger.info("Google Cloud распознал: %s", text)
                            callback(text)
        except Exception as e:
            logger.error("Ошибка Google Cloud STT: %s", e)
            raise

    def stop(self) -> None:
        self._stop_event.set()
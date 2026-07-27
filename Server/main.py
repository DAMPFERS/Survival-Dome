"""
Dome Telemetry System - Main Entry Point
Главная точка входа для системы телеметрии купола.
Объединяет WebSocket-клиент, чат-бот на базе Ollama и движок озвучки.
"""

import argparse
import logging
import sys
import time

from client import DomeTelemetryClient
from dome_chat import DomeChatBot
from audio_writer.tts_engine import TTSEngine


logger = logging.getLogger(__name__)


class DomeSystem:
    """
    Главная система, объединяющая все компоненты.
    """

    def __init__(
        self,
        model: str = "gemma4:latest",
        host: str = "localhost",
        port: int = 8765,
        voice_enabled: bool = True,
        tts_backend: str = "edge",
        tts_voice: str = "ru-RU-SvetlanaNeural",
    ):
        self._voice_enabled = voice_enabled
        
        # Инициализация чат-бота (внутри создаётся DomeTelemetryClient)
        logger.info("Инициализация чат-бота с моделью %s...", model)
        self._bot = DomeChatBot(
            model=model,
            host=host,
            port=port,
        )
        
        # Инициализация TTS
        if voice_enabled:
            logger.info("Инициализация TTS (backend=%s, voice=%s)...", tts_backend, tts_voice)
            self._tts = TTSEngine(
                backend=tts_backend,
                voice=tts_voice,
                volume=0.9,
            )
        else:
            self._tts = None
            logger.info("Озвучка отключена")

    def run_repl(self) -> None:
        """Интерактивный цикл в терминале."""
        print("=" * 70)
        print("DOME TELEMETRY SYSTEM")
        print("=" * 70)
        print("Введите запрос к системе телеметрии купола")
        print("Примеры:")
        print("  - Какая температура в куполе?")
        print("  - Включи третью линию")
        print("  - Какая мощность на 5-м канале?")
        print("  - Выключи реактор")
        print("\nКоманды: 'exit', 'quit', 'выход' — выход из системы")
        print("=" * 70)
        
        while True:
            try:
                user_input = input("\n🧑 Вы: ").strip()
                
                if not user_input:
                    continue
                
                if user_input.lower() in ("exit", "quit", "выход"):
                    print("\n👋 До свидания!")
                    break
                
                # Получаем ответ от бота
                print("\n🤖 Думаю...", end="", flush=True)
                response = self._bot.ask(user_input)
                
                # Выводим ответ в терминал
                print(f"\r🤖 Бот: {response}")
                
                # Озвучиваем ответ (неблокирующе)
                if self._voice_enabled and self._tts:
                    self._tts.speak(response)
                
            except KeyboardInterrupt:
                print("\n\n⚠️  Получен сигнал остановки")
                break
            except Exception as e:
                print(f"\n❌ Ошибка: {e}")
                logger.exception("Ошибка в REPL")

    def stop(self) -> None:
        """Корректно завершает все компоненты."""
        logger.info("Завершение работы системы...")
        
        if self._tts:
            logger.info("Остановка TTS...")
            self._tts.shutdown()
        
        logger.info("Остановка чат-бота и клиента...")
        self._bot.stop()
        
        logger.info("Система остановлена")


def parse_args() -> argparse.Namespace:
    """Парсит аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Dome Telemetry System - чат-бот для управления телеметрией купола",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    
    parser.add_argument(
        "--model",
        type=str,
        default="gemma4:latest",
        help="Имя модели Ollama",
    )
    
    parser.add_argument(
        "--host",
        type=str,
        default="localhost",
        help="Хост WebSocket-сервера",
    )
    
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Порт WebSocket-сервера",
    )
    
    parser.add_argument(
        "--no-voice",
        action="store_true",
        help="Отключить озвучку ответов",
    )
    
    parser.add_argument(
        "--tts-backend",
        type=str,
        choices=["edge", "pyttsx3"],
        default="edge",
        help="Бэкенд TTS (edge — красивые голоса, pyttsx3 — оффлайн)",
    )
    
    parser.add_argument(
        "--tts-voice",
        type=str,
        default="ru-RU-SvetlanaNeural",
        help="Голос TTS (для edge: ru-RU-SvetlanaNeural, ru-RU-DmitryNeural и т.д.)",
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Включить отладочное логирование",
    )
    
    return parser.parse_args()


def main() -> int:
    """Главная функция."""
    args = parse_args()
    
    # Настройка логирования
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    
    # Создаём систему
    try:
        system = DomeSystem(
            model=args.model,
            # host=args.host,
            host="192.168.8.36",
            port=args.port,
            voice_enabled=not args.no_voice,
            tts_backend=args.tts_backend,
            tts_voice=args.tts_voice,
        )
    except Exception as e:
        logger.error("Ошибка инициализации системы: %s", e)
        return 1
    
    # Запускаем REPL
    try:
        system.run_repl()
    except Exception as e:
        logger.exception("Критическая ошибка: %s", e)
        return 1
    finally:
        system.stop()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""
Точка входа: поднимает клиента телеметрии, создаёт агентов, запускает генератор
кризисов и главный цикл оркестратора. Ввод оператора читается в отдельном
потоке, чтобы не блокировать обработку сообщений агентов.

Перед запуском:
    export DEEPSEEK_API_KEY="sk-..."
    python main.py
"""

import logging
import os
import threading

from openai import OpenAI

from agent import Agent
from chat_bus import SharedChatLog
from client import DomeTelemetryClient  # ваш существующий клиент телеметрии
from config import ALL_AGENTS
from crisis_engine import CrisisEngine
from orchestrator import Orchestrator
from relevance import RelevanceScorer
from tool_registry import ToolExecutor

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-pro"   # модель DeepSeek с поддержкой function calling

TELEMETRY_HOST = "192.168.8.36"
TELEMETRY_PORT = 8765


def _wait_for_connection(client: DomeTelemetryClient, timeout_sec: float = 5.0) -> None:
    """Аналог блока ожидания подключения из исходного DomeChatBot.__init__."""
    logger.info("Ожидание подключения к серверу телеметрии...")
    attempts = int(timeout_sec / 0.1)
    for _ in range(attempts):
        if client.is_connected():
            logger.info("Подключение установлено")
            return
        if client.is_failed():
            raise client.get_last_error() or ConnectionError("Не удалось подключиться")
        threading.Event().wait(0.1)
    raise TimeoutError("Не удалось подключиться к серверу телеметрии")


def _operator_input_loop(orchestrator: Orchestrator) -> None:
    """Читает ввод из консоли в отдельном потоке и передаёт реплики оператора в чат."""
    while True:
        try:
            text = input()
        except EOFError:
            break
        text = text.strip()
        if not text:
            continue
        if text.lower() in ("exit", "quit", "выход"):
            os._exit(0)
        orchestrator.submit_operator_message(text)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # api_key = os.environ.get("DEEPSEEK_API_KEY")
    api_key = "sk-537e8011ee764a57b37e8994f9b153d2"
    if not api_key:
        raise RuntimeError("Не задан DEEPSEEK_API_KEY в переменных окружения")

    llm_client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    telemetry_client = DomeTelemetryClient(host=TELEMETRY_HOST, port=TELEMETRY_PORT)
    telemetry_client.start()
    _wait_for_connection(telemetry_client)

    tool_executor = ToolExecutor(telemetry_client)
    bus = SharedChatLog(log_file="dome_chat_log.json")

    agents = [
        Agent(config, llm_client, DEEPSEEK_MODEL, tool_executor, bus)
        for config in ALL_AGENTS
    ]

    scorer = RelevanceScorer(threshold=1)
    orchestrator = Orchestrator(agents, bus, scorer)

    crisis_engine = CrisisEngine(bus, min_interval_sec=90, max_interval_sec=300)
    crisis_engine.start()

    input_thread = threading.Thread(
        target=_operator_input_loop, args=(orchestrator,), daemon=True
    )
    input_thread.start()

    print("=" * 60)
    print("Купол выживания: чат агентов запущен")
    print("Пишите реплики оператора в консоль. 'exit' — выход.")
    print("=" * 60)

    try:
        orchestrator.run()
    finally:
        crisis_engine.stop()
        telemetry_client.stop()


if __name__ == "__main__":
    main()

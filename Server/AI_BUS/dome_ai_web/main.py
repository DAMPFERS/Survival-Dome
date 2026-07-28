"""Точка входа мультиагентного купола с веб-интерфейсом."""
import logging, os, threading
from openai import OpenAI
from agent import Agent
from chat_bus import SharedChatLog
from client import DomeTelemetryClient
from config import ALL_AGENTS, COORDINATOR_AGENT
from crisis_engine import CrisisEngine
from orchestrator import Orchestrator
from relevance import RelevanceScorer
from tool_registry import ToolExecutor
from web_bridge import WebBridge

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
TELEMETRY_HOST = os.getenv("TELEMETRY_HOST", "192.168.8.36")
TELEMETRY_PORT = int(os.getenv("TELEMETRY_PORT", "8765"))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8766"))


def wait_for_connection(client, timeout=15.0):
    logging.info("Ожидание телеметрии %s:%s", TELEMETRY_HOST, TELEMETRY_PORT)
    if not client._connected.wait(timeout):
        raise TimeoutError("Сервер телеметрии недоступен")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # api_key = os.getenv("DEEPSEEK_API_KEY")
    api_key = "sk-537e8011ee764a57b37e8994f9b153d2"
    if not api_key:
        raise RuntimeError("Задайте DEEPSEEK_API_KEY")

    llm = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
    telemetry = DomeTelemetryClient(TELEMETRY_HOST, TELEMETRY_PORT)
    telemetry.start()
    wait_for_connection(telemetry)

    tools = ToolExecutor(telemetry)
    bus = SharedChatLog("dome_chat_log.json")
    agents = [Agent(cfg, llm, DEEPSEEK_MODEL, tools, bus) for cfg in ALL_AGENTS]
    orchestrator = Orchestrator(agents, bus, RelevanceScorer(threshold=1))
    bridge = WebBridge(bus, orchestrator, tools, COORDINATOR_AGENT.name, WEB_HOST, WEB_PORT)
    crisis = CrisisEngine(bus, 90, 300)

    bridge.start(); crisis.start()
    bus.append("Система", "Мультиагентное управление куполом запущено.")
    logging.info("Откройте index.html через HTTP-сервер; WebSocket: ws://localhost:%d", WEB_PORT)
    try:
        orchestrator.run()
    finally:
        crisis.stop(); bridge.stop(); telemetry.stop()

if __name__ == "__main__":
    main()

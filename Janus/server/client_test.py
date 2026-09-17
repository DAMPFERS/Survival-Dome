#!/usr/bin/env python3
"""
Диагностический WebSocket-клиент телеметрии купола.
"""

import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

HOST = "survival-dom.ru.tuna.am"
PORT = 443
URI = f"wss://{HOST}:{PORT}"
RECONNECT_DELAY = 3.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TelemetryClient")


def print_telemetry(data: dict) -> None:
    """Безопасный вывод телеметрии."""
    try:
        timestamp = data.get("timestamp", "—")
        payload = data.get("data", {})

        nodes = payload.get("nodes", {})
        node_modes = payload.get("node_modes", {})
        active_crises = payload.get("active_crises", [])
        sim_time = payload.get("sim_time", 0)
        time_scale = payload.get("time_scale", 1.0)

        print("\n" + "=" * 80)
        print(f"  TELEMETRY  |  {datetime.now().strftime('%H:%M:%S')}  |  server_ts: {timestamp}")
        print("=" * 80)
        print(f"⏱  Simulation time : {sim_time}")
        print(f"⏩  Time scale      : ×{time_scale}")
        print(f"🚨  Active crises  : {active_crises or 'none'}")
        print(f"\n🔧  Node modes ({len(node_modes)}):")
        for nid, mode in sorted(node_modes.items()):
            print(f"    {nid:<25} → {mode}")

        print(f"\n📊  Nodes ({len(nodes)}):")
        for node_id, state in sorted(nodes.items()):
            mode = node_modes.get(node_id, "?")
            print(f"\n  ┌─ {node_id}  [{mode}]")
            if isinstance(state, dict):
                for k, v in sorted(state.items()):
                    print(f"  │  {k:<30} : {v}")
            else:
                print(f"  │  {state}")
            print("  └" + "─" * 50)
        print("=" * 80 + "\n")

    except Exception as e:
        logger.exception(f"Ошибка при форматировании телеметрии: {e}")
        print("RAW DATA:", json.dumps(data, ensure_ascii=False, indent=2)[:2000])


async def receive_telemetry(websocket) -> None:
    """Приём сообщений с защитой от падений."""
    async for raw_message in websocket:
        logger.info(f"Получено сообщение ({len(raw_message)} байт)")
        try:
            data = json.loads(raw_message)
            msg_type = data.get("type")
            logger.info(f"Тип сообщения: {msg_type}")

            if msg_type == "telemetry":
                print_telemetry(data)
            else:
                print("Не telemetry:", json.dumps(data, ensure_ascii=False, indent=2)[:1500])

        except json.JSONDecodeError:
            logger.error(f"Невалидный JSON: {raw_message[:300]}")
        except Exception as e:
            logger.exception(f"Ошибка обработки сообщения: {e}")


async def run_client() -> None:
    attempt = 0
    while True:
        attempt += 1
        try:
            logger.info(f"Подключение к {URI} (попытка #{attempt})...")

            async with websockets.connect(
                URI,
                ping_interval=None,      # временно отключаем ping для диагностики
                close_timeout=3,
                max_size=10_000_000,
            ) as websocket:
                logger.info("Успешно подключено. Ожидаю сообщения...")
                attempt = 0
                await receive_telemetry(websocket)

        except ConnectionClosed as e:
            logger.warning(f"Соединение закрыто сервером: code={e.code}, reason={e.reason}")
        except WebSocketException as e:
            logger.warning(f"WebSocketException: {e}")
        except ConnectionRefusedError:
            logger.error("Сервер не запущен или порт закрыт")
        except Exception as e:
            logger.exception(f"Неожиданная ошибка: {e}")

        logger.info(f"Повтор через {RECONNECT_DELAY} сек...\n")
        await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        print("\nКлиент остановлен.")
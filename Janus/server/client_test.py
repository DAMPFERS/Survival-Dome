import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import Any
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

# ============================================================
# CONFIG
# ============================================================
HOST = "localhost"
PORT = 8765
URI = f"ws://{HOST}:{PORT}"

# Таймаут ожидания ответа сервера
COMMAND_TIMEOUT = 5.0
# Сколько ждём первую телеметрию перед отправкой команд
TELEMETRY_WAIT = 5.0

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================
# GLOBAL STATE
# ============================================================
response_waiters: dict[str, asyncio.Future] = {}
telemetry_received = asyncio.Event()
last_telemetry: dict[str, Any] = {}


# ============================================================
# TELEMETRY
# ============================================================
def print_telemetry(data: dict[str, Any]) -> None:
    """Вывод телеметрии сервера."""
    print("\n" + "=" * 70)
    print("📡 TELEMETRY")
    print("=" * 70)

    timestamp = data.get("timestamp")
    if timestamp:
        try:
            dt = datetime.fromtimestamp(timestamp)
            print(f"Time:        {dt}")
        except Exception:
            print(f"Timestamp:   {timestamp}")

    payload = data.get("data", data)
    print(f"Sim time:    {payload.get('sim_time')}")
    print(f"Time scale:  {payload.get('time_scale')}")

    nodes = payload.get("nodes", {})
    node_modes = payload.get("node_modes", {})
    active_crises = payload.get("active_crises", [])

    print("\nNodes:")
    if isinstance(nodes, dict):
        for node_id, node_data in nodes.items():
            mode = node_modes.get(node_id, "?")
            if isinstance(node_data, dict):
                print(
                    f"  {node_id:<22} "
                    f"mode={mode:<7} "
                    f"{json.dumps(node_data, ensure_ascii=False)}"
                )
            else:
                print(
                    f"  {node_id:<22} "
                    f"mode={mode:<7} "
                    f"{node_data}"
                )

    print("\nActive crises:")
    if active_crises:
        for crisis in active_crises:
            print(f"  - {crisis}")
    else:
        print("  none")

    print("=" * 70)


# ============================================================
# SERVER RESPONSE
# ============================================================
def handle_response(data: dict[str, Any]) -> None:
    """Обработка ответа сервера."""
    msg_type = data.get("type")
    if not msg_type:
        return

    waiter = response_waiters.get(msg_type)
    if waiter is not None and not waiter.done():
        waiter.set_result(data)
        return

    print("\nSERVER RESPONSE (unsolicited):")
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ============================================================
# RECEIVE LOOP
# ============================================================
async def receive_messages(websocket) -> None:
    """Читает все входящие сообщения."""
    async for raw_message in websocket:
        logger.debug("Received: %s", raw_message)
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("Получен невалидный JSON: %s", raw_message)
            continue

        msg_type = data.get("type")
        if msg_type == "telemetry":
            global last_telemetry
            last_telemetry = data
            print_telemetry(data)
            if not telemetry_received.is_set():
                telemetry_received.set()
        elif msg_type and msg_type.endswith("_response"):
            handle_response(data)
        else:
            print("\nSERVER MESSAGE:")
            print(json.dumps(data, ensure_ascii=False, indent=2))


# ============================================================
# SEND COMMAND
# ============================================================
async def send_command(
    websocket,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    """Отправляет команду и ждёт подтверждение от сервера."""
    msg_type = data.get("type")
    if not msg_type:
        print("❌ Ошибка: у команды отсутствует type")
        return None

    response_type = f"{msg_type}_response"
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    response_waiters[response_type] = future

    try:
        message = json.dumps(data, ensure_ascii=False)
        print("\n" + "-" * 70)
        print("📤 CLIENT -> SERVER:")
        print(json.dumps(data, ensure_ascii=False, indent=2))

        await websocket.send(message)

        try:
            response = await asyncio.wait_for(
                future,
                timeout=COMMAND_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"\n⏱ Нет ответа сервера за {COMMAND_TIMEOUT} секунд.")
            return None

        print("\n📥 SERVER -> CLIENT:")
        print(json.dumps(response, ensure_ascii=False, indent=2))
        print("-" * 70)
        return response

    except ConnectionClosed:
        print("\n❌ Соединение закрыто сервером.")
        raise
    except Exception as e:
        print(f"\n❌ Ошибка отправки команды: {e}")
        return None
    finally:
        current = response_waiters.get(response_type)
        if current is future:
            del response_waiters[response_type]


# ============================================================
# TEST SCENARIO
# ============================================================
async def run_test_scenario(websocket) -> None:
    """
    Автоматический сценарий:
    1. Ждём первую телеметрию
    2. Включаем дизель-генератор
    3. Включаем линию line_1
    """
    print("\n" + "#" * 70)
    print("# TEST SCENARIO START")
    print("#" * 70)

    # Шаг 1: ждём первую телеметрию
    print("\n⏳ Ожидание первой телеметрии...")
    try:
        await asyncio.wait_for(
            telemetry_received.wait(),
            timeout=TELEMETRY_WAIT,
        )
        print("✅ Телеметрия получена.")
    except asyncio.TimeoutError:
        print("⚠️ Телеметрия не пришла вовремя, продолжаем...")

    # Дадим ещё немного времени, чтобы телеметрия успела отрисоваться
    await asyncio.sleep(0.5)

    # Шаг 2: включаем дизель-генератор
    print("\n" + "#" * 70)
    print("# STEP 2: Запуск дизель-генератора (diesel_1)")
    print("#" * 70)
    diesel_response = await send_command(
        websocket,
        {
            "type": "control",
            "node_id": "diesel_1",
            "action": "start",
        },
    )
    if diesel_response and diesel_response.get("success"):
        print("✅ Дизель-генератор успешно запущен.")
    else:
        print("❌ Не удалось запустить дизель-генератор.")

    # Дадим симулятору тикнуть, чтобы состояние обновилось
    await asyncio.sleep(5.0)

    # Шаг 3: включаем линию line_1
    print("\n" + "#" * 70)
    print("# STEP 3: Включение линии line_1")
    print("#" * 70)
    line_response = await send_command(
        websocket,
        {
            "type": "control",
            "node_id": "line_1",
            "action": "enable",
        },
    )
    if line_response and line_response.get("success"):
        print("✅ Линия line_1 успешно включена.")
    else:
        print("❌ Не удалось включить линию line_1.")

    # Дадим симулятору отрисовать новое состояние
    await asyncio.sleep(5.0)

    print("\n" + "#" * 70)
    print("# TEST SCENARIO COMPLETE")
    print("#" * 70)


# ============================================================
# CONNECTION SESSION
# ============================================================
async def run_session(websocket) -> None:
    """Работа с одним WebSocket-соединением."""
    receiver_task = asyncio.create_task(receive_messages(websocket))
    scenario_task = asyncio.create_task(run_test_scenario(websocket))

    done, pending = await asyncio.wait(
        [receiver_task, scenario_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        exception = task.exception()
        if exception is not None:
            raise exception


# ============================================================
# MAIN CLIENT
# ============================================================
async def run_client() -> None:
    attempt = 0
    while True:
        try:
            attempt += 1
            logger.info("Подключение к %s (попытка %d)...", URI, attempt)
            async with websockets.connect(
                URI,
                ping_interval=None,
                close_timeout=3,
                max_size=10_000_000,
            ) as websocket:
                logger.info("✅ Успешно подключено к серверу.")
                attempt = 0
                await run_session(websocket)
                # Сценарий выполнен — выходим
                return
        except KeyboardInterrupt:
            raise
        except ConnectionClosed as e:
            logger.warning("Соединение закрыто: code=%s reason=%s", e.code, e.reason)
        except WebSocketException as e:
            logger.warning("WebSocket ошибка: %s", e)
        except OSError as e:
            logger.warning("Ошибка сети: %s", e)
        except Exception as e:
            logger.exception("Неожиданная ошибка клиента: %s", e)

        for future in response_waiters.values():
            if not future.done():
                future.cancel()
        response_waiters.clear()
        telemetry_received.clear()

        logger.info("Повторное подключение через 3.0 сек...")
        await asyncio.sleep(3.0)


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        print("\nКлиент остановлен.")
        sys.exit(0)
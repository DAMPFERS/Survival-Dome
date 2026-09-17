"""
WebSocket клиент для подключения к серверу купола (server_integrated.py).

Функционал:
- Асинхронное подключение к серверу купола
- Получение телеметрии в реальном времени
- Отправка управляющих команд с получением подтверждения
- Автоматическое переподключение при потере связи
- Thread-safe доступ к данным телеметрии
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
except ImportError:
    # websockets уже в requirements.txt
    pass

# Совместимость: в приложении берём settings, при автономном запуске — дефолты
try:
    from app.config import settings

    _DEFAULT_URL = settings.DOME_SERVER_URL
    _DEFAULT_RECONNECT = settings.DOME_RECONNECT_DELAY
    _DEFAULT_ENABLE_CONTROL = settings.DOME_ENABLE_CONTROL
except Exception:
    _DEFAULT_URL = os.getenv("DOME_SERVER_URL", "ws://localhost:8765")
    _DEFAULT_RECONNECT = float(os.getenv("DOME_RECONNECT_DELAY", "3.0"))
    _DEFAULT_ENABLE_CONTROL = os.getenv("DOME_ENABLE_CONTROL", "1") not in (
        "0",
        "false",
        "False",
        "",
    )

logger = logging.getLogger(__name__)


class DomeClient:
    """WebSocket клиент для взаимодействия с сервером купола."""

    # Ключевые узлы для отображения на дашборде
    KEY_NODES = [
        # Энергетика
        "solar_1",
        "battery_1",
        "diesel_1",
        # Линии передачи
        "line_1",
        "line_2",
        "line_3",
        "line_4",
        "line_5",
        "line_6",
        "line_7",
        "line_8",
        # Климат
        "climate_residential",
        "co2_sensor_1",
    ]

    def __init__(
        self,
        url: str | None = None,
        reconnect_delay: float | None = None,
        enable_control: bool | None = None,
        response_timeout: float = 10.0,
    ):
        self.url = url if url is not None else _DEFAULT_URL
        self.reconnect_delay = (
            reconnect_delay if reconnect_delay is not None else _DEFAULT_RECONNECT
        )
        self.enable_control = (
            enable_control if enable_control is not None else _DEFAULT_ENABLE_CONTROL
        )

        self._ws: WebSocketClientProtocol | None = None
        self._latest_telemetry: dict[str, Any] | None = None
        self._connected = False
        self._running = False
        self._lock = asyncio.Lock()
        self._telemetry_event = asyncio.Event()

        # Для отслеживания ответов на команды (ключ: "node_id:action")
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._response_timeout = response_timeout

        logger.info(
            "DomeClient initialized (URL: %s, control: %s)",
            self.url,
            self.enable_control,
        )

    async def connect(self) -> None:
        """Подключение к серверу купола с автоматическим переподключением."""
        if self._running:
            logger.warning("DomeClient already running")
            return

        self._running = True
        self._telemetry_event.clear()
        asyncio.create_task(self._connection_loop())
        logger.info("DomeClient connection loop started")

    async def disconnect(self) -> None:
        """Отключение от сервера купола."""
        logger.info("DomeClient disconnecting...")
        self._running = False

        # Отменяем все ожидающие ответы
        for key, future in list(self._pending_responses.items()):
            if not future.done():
                future.cancel()
            self._pending_responses.pop(key, None)

        if self._ws:
            try:
                await self._ws.close()
            except Exception as e:
                logger.error("Error closing WebSocket: %s", e)

        self._ws = None
        self._connected = False
        logger.info("DomeClient disconnected")

    async def wait_for_telemetry(self, timeout: float = 10.0) -> bool:
        """
        Дождаться первой (или очередной) телеметрии.

        Returns:
            True если телеметрия пришла, False при таймауте.
        """
        try:
            await asyncio.wait_for(self._telemetry_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _connection_loop(self) -> None:
        """Основной цикл подключения с автоматическим переподключением."""
        while self._running:
            try:
                logger.info("Connecting to dome server at %s...", self.url)
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=10_000_000,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("✅ Connected to dome server")

                    # Основной цикл приема сообщений
                    async for message in ws:
                        await self._handle_message(message)

            except asyncio.CancelledError:
                logger.info("Connection loop cancelled")
                break
            except Exception as e:
                logger.error("Connection error: %s", e)
                self._connected = False
                self._ws = None

                if self._running:
                    logger.info(
                        "Reconnecting in %.1f seconds...", self.reconnect_delay
                    )
                    await asyncio.sleep(self.reconnect_delay)

    async def _handle_message(self, message: str) -> None:
        """Обработка входящего сообщения от сервера купола."""
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "telemetry":
                await self._handle_telemetry(data)

            elif msg_type == "control_response":
                await self._handle_control_response(data)

            elif msg_type == "switch_mode_response":
                # Ответ на переключение режима узла (не используем пока)
                logger.debug("Switch mode response: %s", data)

            else:
                logger.debug("Unknown message type: %s", msg_type)

        except json.JSONDecodeError as e:
            logger.error("Failed to parse message: %s", e)
        except Exception as e:
            logger.error("Error handling message: %s", e)

    async def _handle_telemetry(self, data: dict) -> None:
        """Обработка сообщения с телеметрией."""
        async with self._lock:
            # Нормализуем: берём payload + сохраняем timestamp с верхнего уровня,
            # если его нет внутри data
            payload = data.get("data", data)
            if isinstance(payload, dict):
                if "timestamp" not in payload and "timestamp" in data:
                    payload = {**payload, "timestamp": data["timestamp"]}
                self._latest_telemetry = payload
            else:
                self._latest_telemetry = data

            self._telemetry_event.set()
            nodes = self._latest_telemetry.get("nodes", {}) if self._latest_telemetry else {}
            logger.debug("Telemetry updated: %d nodes", len(nodes))

    async def _handle_control_response(self, data: dict) -> None:
        """Обработка ответа на управляющую команду."""
        node_id = data.get("node_id")
        action = data.get("action")
        key = f"{node_id}:{action}"

        future = self._pending_responses.pop(key, None)

        # Fallback: если сервер не вернул node_id/action, но есть ровно один
        # ожидающий ответ — отдаём его (совместимость с простым протоколом)
        if future is None and len(self._pending_responses) == 1:
            only_key = next(iter(self._pending_responses))
            future = self._pending_responses.pop(only_key)
            logger.debug(
                "Control response matched by fallback (only pending): %s", only_key
            )

        if future is not None and not future.done():
            future.set_result(data)
            logger.debug(
                "Control response received: %s -> success=%s",
                key,
                data.get("success"),
            )
        else:
            logger.debug("Unsolicited control_response: %s", data)

    async def send_control_command(
        self,
        node_id: str,
        action: str,
        **params: Any,
    ) -> dict[str, Any]:
        """
        Отправка управляющей команды на сервер купола.

        Args:
            node_id: Идентификатор узла (например, "line_1", "diesel_1")
            action: Действие ("enable", "disable", "start", "stop", и т.д.)
            **params: Дополнительные параметры (например, value, duration_s)

        Returns:
            Ответ сервера с полями:
            - success: bool
            - node_id: str
            - action: str
            - mode: str (real/virtual)
            - error: str (если success=False)

        Raises:
            RuntimeError: Если нет подключения или управление отключено
            asyncio.TimeoutError: Если сервер не ответил вовремя
        """
        if not self.enable_control:
            raise RuntimeError("Dome control is disabled in configuration")

        if not self.is_connected():
            raise RuntimeError("Not connected to dome server")

        command = {
            "type": "control",
            "node_id": node_id,
            "action": action,
            **params,
        }

        key = f"{node_id}:{action}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_responses[key] = future

        try:
            await self._ws.send(json.dumps(command, ensure_ascii=False))
            logger.info("Control command sent: %s.%s %s", node_id, action, params)

            response = await asyncio.wait_for(
                future, timeout=self._response_timeout
            )
            return response

        except asyncio.TimeoutError:
            self._pending_responses.pop(key, None)
            logger.error("Control command timeout: %s", key)
            raise

        except Exception as e:
            self._pending_responses.pop(key, None)
            logger.error("Error sending control command: %s", e)
            raise

    def get_latest_telemetry(self) -> dict[str, Any] | None:
        """
        Получить последние данные телеметрии.

        Returns:
            Словарь с полями:
            - nodes: dict[str, dict] - состояние всех узлов
            - active_crises: list[str] - активные кризисы
            - sim_time: float - время симуляции
            - time_scale: float - скорость симуляции
            - node_modes: dict[str, str] - режимы узлов (real/virtual)
        """
        return self._latest_telemetry

    def get_key_nodes(self) -> dict[str, Any] | None:
        """
        Получить данные только ключевых узлов для отображения на дашборде.

        Returns:
            Словарь с полями:
            - nodes: dict - только ключевые узлы
            - active_crises: list[str]
            - sim_time: float
            - timestamp: float
            - connected: bool
        """
        if not self._latest_telemetry:
            return None

        all_nodes = self._latest_telemetry.get("nodes", {})
        key_nodes = {
            node_id: all_nodes[node_id]
            for node_id in self.KEY_NODES
            if node_id in all_nodes
        }

        return {
            "nodes": key_nodes,
            "active_crises": self._latest_telemetry.get("active_crises", []),
            "sim_time": self._latest_telemetry.get("sim_time", 0.0),
            "timestamp": self._latest_telemetry.get("timestamp", 0.0),
            "connected": self._connected,
        }

    def get_node_status(self, node_id: str) -> dict[str, Any] | None:
        """
        Получить статус конкретного узла.

        Args:
            node_id: Идентификатор узла

        Returns:
            Словарь с данными узла или None, если узел не найден
        """
        if not self._latest_telemetry:
            return None

        nodes = self._latest_telemetry.get("nodes", {})
        return nodes.get(node_id)

    def is_connected(self) -> bool:
        """Проверка состояния подключения к серверу купола."""
        return self._connected and self._ws is not None

    def get_connection_info(self) -> dict[str, Any]:
        """Получить информацию о подключении."""
        ts = None
        if self._latest_telemetry and self._latest_telemetry.get("timestamp"):
            try:
                ts = datetime.fromtimestamp(
                    self._latest_telemetry["timestamp"]
                ).isoformat()
            except (TypeError, ValueError, OSError):
                ts = str(self._latest_telemetry.get("timestamp"))

        return {
            "url": self.url,
            "connected": self._connected,
            "running": self._running,
            "enable_control": self.enable_control,
            "last_telemetry": ts,
        }


# Глобальный экземпляр клиента (совместимость с существующим кодом приложения)
dome_client = DomeClient()


# ---------------------------------------------------------------------------
# Вспомогательный вывод телеметрии (для автономного теста)
# ---------------------------------------------------------------------------
def _print_telemetry(payload: dict[str, Any] | None) -> None:
    print("\n" + "=" * 70)
    print("📡 TELEMETRY")
    print("=" * 70)

    if not payload:
        print("  (пусто)")
        print("=" * 70)
        return

    timestamp = payload.get("timestamp")
    if timestamp:
        try:
            print(f"Time:        {datetime.fromtimestamp(timestamp)}")
        except (TypeError, ValueError, OSError):
            print(f"Timestamp:   {timestamp}")

    print(f"Sim time:    {payload.get('sim_time')}")
    print(f"Time scale:  {payload.get('time_scale')}")

    nodes = payload.get("nodes", {})
    node_modes = payload.get("node_modes", {})
    active_crises = payload.get("active_crises", [])

    print("\nNodes:")
    if isinstance(nodes, dict) and nodes:
        for node_id, node_data in nodes.items():
            mode = node_modes.get(node_id, "?")
            if isinstance(node_data, dict):
                print(
                    f"  {node_id:<22} "
                    f"mode={mode:<7} "
                    f"{json.dumps(node_data, ensure_ascii=False)}"
                )
            else:
                print(f"  {node_id:<22} mode={mode:<7} {node_data}")
    else:
        print("  (нет узлов)")

    print("\nActive crises:")
    if active_crises:
        for crisis in active_crises:
            print(f"  - {crisis}")
    else:
        print("  none")

    print("=" * 70)


# ---------------------------------------------------------------------------
# Тестовый сценарий (аналог client_test.py)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    async def run_test_scenario() -> None:
        client = DomeClient(
            url=os.getenv("DOME_SERVER_URL", "ws://localhost:8765"),
            enable_control=True,
            response_timeout=5.0,
        )

        print("\n" + "#" * 70)
        print("# TEST SCENARIO START (dome_client)")
        print("#" * 70)

        await client.connect()

        # --- Шаг 1: ждём первую телеметрию ---
        print("\n⏳ Ожидание первой телеметрии...")
        ok = await client.wait_for_telemetry(timeout=10.0)
        if ok:
            print("✅ Телеметрия получена.")
            _print_telemetry(client.get_latest_telemetry())
        else:
            print("⚠️ Телеметрия не пришла вовремя, продолжаем...")

        await asyncio.sleep(0.5)

        # --- Шаг 2: запуск дизель-генератора ---
        print("\n" + "#" * 70)
        print("# STEP 2: Запуск дизель-генератора (diesel_1)")
        print("#" * 70)
        try:
            diesel_response = await client.send_control_command(
                "diesel_1", "start"
            )
            print("\n📥 SERVER RESPONSE:")
            print(json.dumps(diesel_response, ensure_ascii=False, indent=2))
            if diesel_response.get("success"):
                print("✅ Дизель-генератор успешно запущен.")
            else:
                print("❌ Не удалось запустить дизель-генератор.")
        except asyncio.TimeoutError:
            print("❌ Таймаут ожидания ответа на start diesel_1")
        except Exception as e:
            print(f"❌ Ошибка: {e}")

        # Даём симулятору обновить состояние
        await asyncio.sleep(5.0)
        print("\n📡 Телеметрия после запуска дизеля:")
        _print_telemetry(client.get_latest_telemetry())

        # --- Шаг 3: включение линии line_1 ---
        print("\n" + "#" * 70)
        print("# STEP 3: Включение линии line_1")
        print("#" * 70)
        try:
            line_response = await client.send_control_command(
                "line_1", "enable"
            )
            print("\n📥 SERVER RESPONSE:")
            print(json.dumps(line_response, ensure_ascii=False, indent=2))
            if line_response.get("success"):
                print("✅ Линия line_1 успешно включена.")
            else:
                print("❌ Не удалось включить линию line_1.")
        except asyncio.TimeoutError:
            print("❌ Таймаут ожидания ответа на enable line_1")
        except Exception as e:
            print(f"❌ Ошибка: {e}")

        await asyncio.sleep(5.0)
        print("\n📡 Телеметрия после включения line_1:")
        _print_telemetry(client.get_latest_telemetry())

        print("\n" + "#" * 70)
        print("# TEST SCENARIO COMPLETE")
        print("#" * 70)

        await client.disconnect()

    try:
        asyncio.run(run_test_scenario())
    except KeyboardInterrupt:
        print("\nКлиент остановлен.")
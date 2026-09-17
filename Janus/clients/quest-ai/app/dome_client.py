"""
WebSocket клиент для подключения к серверу купола (server_integrated.py).

Функционал:
- Асинхронное подключение к серверу купола
- Получение телеметрии в реальном времени
- Отправка управляющих команд с получением подтверждения
- Автоматическое переподключение при потере связи
- Thread-safe доступ к данным телеметрии
"""
import asyncio
import json
import logging
from typing import Any, Callable
from datetime import datetime

try:
    import websockets
    from websockets.client import WebSocketClientProtocol
except ImportError:
    # websockets уже в requirements.txt
    pass

from app.config import settings

logger = logging.getLogger(__name__)


class DomeClient:
    """WebSocket клиент для взаимодействия с сервером купола."""

    # Ключевые узлы для отображения на дашборде
    KEY_NODES = [
        # Энергетика
        "solar_1", "battery_1", "diesel_1",
        # Линии передачи
        "line_1", "line_2", "line_3", "line_4",
        "line_5", "line_6", "line_7", "line_8",
        # Климат
        "climate_residential", "co2_sensor_1",
    ]

    def __init__(self):
        self.url = settings.DOME_SERVER_URL
        self.reconnect_delay = settings.DOME_RECONNECT_DELAY
        self.enable_control = settings.DOME_ENABLE_CONTROL

        self._ws: WebSocketClientProtocol | None = None
        self._latest_telemetry: dict[str, Any] | None = None
        self._connected = False
        self._running = False
        self._lock = asyncio.Lock()
        
        # Для отслеживания ответов на команды
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._response_timeout = 10.0  # секунд

        logger.info(f"DomeClient initialized (URL: {self.url}, control: {self.enable_control})")

    async def connect(self) -> None:
        """Подключение к серверу купола с автоматическим переподключением."""
        if self._running:
            logger.warning("DomeClient already running")
            return

        self._running = True
        asyncio.create_task(self._connection_loop())
        logger.info("DomeClient connection loop started")

    async def disconnect(self) -> None:
        """Отключение от сервера купола."""
        logger.info("DomeClient disconnecting...")
        self._running = False

        if self._ws:
            try:
                await self._ws.close()
            except Exception as e:
                logger.error(f"Error closing WebSocket: {e}")

        self._ws = None
        self._connected = False
        logger.info("DomeClient disconnected")

    async def _connection_loop(self) -> None:
        """Основной цикл подключения с автоматическим переподключением."""
        while self._running:
            try:
                logger.info(f"Connecting to dome server at {self.url}...")
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=10,
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
                logger.error(f"Connection error: {e}")
                self._connected = False
                self._ws = None

                if self._running:
                    logger.info(f"Reconnecting in {self.reconnect_delay} seconds...")
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
                logger.debug(f"Switch mode response: {data}")

            else:
                logger.debug(f"Unknown message type: {msg_type}")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse message: {e}")
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    async def _handle_telemetry(self, data: dict) -> None:
        """Обработка сообщения с телеметрией."""
        async with self._lock:
            self._latest_telemetry = data.get("data", {})
            logger.debug(f"Telemetry updated: {len(self._latest_telemetry.get('nodes', {}))} nodes")

    async def _handle_control_response(self, data: dict) -> None:
        """Обработка ответа на управляющую команду."""
        node_id = data.get("node_id")
        action = data.get("action")
        key = f"{node_id}:{action}"

        if key in self._pending_responses:
            future = self._pending_responses.pop(key)
            if not future.done():
                future.set_result(data)
                logger.debug(f"Control response received: {key} -> {data.get('success')}")

    async def send_control_command(
        self,
        node_id: str,
        action: str,
        **params
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

        # Формируем команду
        command = {
            "type": "control",
            "node_id": node_id,
            "action": action,
            **params
        }

        # Создаем Future для ожидания ответа
        key = f"{node_id}:{action}"
        future = asyncio.Future()
        self._pending_responses[key] = future

        try:
            # Отправляем команду
            await self._ws.send(json.dumps(command))
            logger.info(f"Control command sent: {node_id}.{action} {params}")

            # Ожидаем ответ с таймаутом
            response = await asyncio.wait_for(future, timeout=self._response_timeout)
            return response

        except asyncio.TimeoutError:
            self._pending_responses.pop(key, None)
            logger.error(f"Control command timeout: {key}")
            raise

        except Exception as e:
            self._pending_responses.pop(key, None)
            logger.error(f"Error sending control command: {e}")
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
        return {
            "url": self.url,
            "connected": self._connected,
            "running": self._running,
            "enable_control": self.enable_control,
            "last_telemetry": (
                datetime.fromtimestamp(self._latest_telemetry.get("timestamp", 0)).isoformat()
                if self._latest_telemetry and self._latest_telemetry.get("timestamp")
                else None
            ),
        }


# Глобальный экземпляр клиента
dome_client = DomeClient()


if __name__ == "__main__":
    # Пример использования клиента
    async def main():
        await dome_client.connect()
        await asyncio.sleep(5)  # Ждем немного, чтобы получить телеметрию
        telemetry = dome_client.get_latest_telemetry()
        print("Latest telemetry:", telemetry)
        await dome_client.disconnect()
        
        await asyncio.sleep(5)
        
        t = dome_client.get_key_nodes()
        print("Key nodes:", t)
        
        try:
            result = await dome_client.send_control_command("line_1", "enable")
            if result.get("success"):
                print("Control command succeeded:", result)
            else:
                print("Control command failed:", result)
        except RuntimeError as e:
            print("Error:", e)

    asyncio.run(main())
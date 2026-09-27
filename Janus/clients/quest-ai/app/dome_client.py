"""
WebSocket клиент для подключения к серверу купола (server_integrated.py).

Функционал:
- Авторизация ключом доступа (первое сообщение соединения)
- Получение телеметрии в реальном времени
- Отправка управляющих команд и запросов с сопоставлением ответов по request_id
- Каталог узлов (describe_nodes) — какие действия доступны
- Автоматическое переподключение при потере связи
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from datetime import datetime
from typing import Any

import websockets

# Совместимость: в приложении берём settings, при автономном запуске — дефолты
try:
    from app.config import settings

    _DEFAULT_URL = settings.DOME_SERVER_URL
    _DEFAULT_RECONNECT = settings.DOME_RECONNECT_DELAY
    _DEFAULT_ENABLE_CONTROL = settings.DOME_ENABLE_CONTROL
    _DEFAULT_KEY = settings.DOME_ACCESS_KEY
except Exception:
    _DEFAULT_URL = os.getenv("DOME_SERVER_URL", "ws://localhost:8765")
    _DEFAULT_RECONNECT = float(os.getenv("DOME_RECONNECT_DELAY", "3.0"))
    _DEFAULT_ENABLE_CONTROL = os.getenv("DOME_ENABLE_CONTROL", "1") not in ("0", "false", "False", "")
    _DEFAULT_KEY = os.getenv("DOME_ACCESS_KEY", "")

logger = logging.getLogger(__name__)

# Пауза перед повтором, если сервер отверг ключ (чтобы не долбить сервер)
AUTH_RETRY_DELAY = 30.0


class DomeAuthError(RuntimeError):
    """Сервер купола отверг ключ доступа."""


class DomeClient:
    """WebSocket клиент для взаимодействия с сервером купола."""

    # Ключевые узлы для промпта Хранителя
    KEY_NODES = [
        # Энергетика
        "solar_panels_01",
        "solar_inverter_01",
        "battery_01",
        "wind_turbine_01",
        "dizel_1",
        "fuel_tank_01",
        # Электроснабжение
        "smart_panel_01",
        # Производство
        "printer_3d_01",
        "cnc_01",
        # Климат и безопасность
        "climate_sensor_01",
        "air_quality_sensor_01",
        "smoke_detector_01",
        "supply_ventilation_01",
        "fume_extraction_01",
        # Вода
        "water_tank_01",
        # Внешняя среда и угрозы
        "weather_station_01",
        "radiation_sensor_01",
        "chem_sensor_01",
    ]

    def __init__(
        self,
        url: str | None = None,
        access_key: str | None = None,
        reconnect_delay: float | None = None,
        enable_control: bool | None = None,
        response_timeout: float = 10.0,
    ):
        self.url = url if url is not None else _DEFAULT_URL
        self.access_key = access_key if access_key is not None else _DEFAULT_KEY
        self.reconnect_delay = reconnect_delay if reconnect_delay is not None else _DEFAULT_RECONNECT
        self.enable_control = enable_control if enable_control is not None else _DEFAULT_ENABLE_CONTROL

        self._ws = None
        self._latest_telemetry: dict[str, Any] | None = None
        self._catalog: dict[str, dict[str, Any]] = {}
        self._connected = False
        self._running = False
        self._role: str | None = None
        self._auth_error: str | None = None
        self._task: asyncio.Task | None = None
        self._telemetry_event = asyncio.Event()

        self._pending: dict[str, asyncio.Future] = {}
        self._request_ids = itertools.count(1)
        self._response_timeout = response_timeout

        logger.info("DomeClient initialized (URL: %s, control: %s, key: %s)",
                    self.url, self.enable_control, "задан" if self.access_key else "НЕ ЗАДАН")

    # ------------------------------------------------------------------
    # Подключение
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Запуск цикла подключения с автоматическим переподключением."""
        if self._running:
            logger.warning("DomeClient already running")
            return
        self._running = True
        self._telemetry_event.clear()
        self._task = asyncio.create_task(self._connection_loop())
        logger.info("DomeClient connection loop started")

    async def disconnect(self) -> None:
        logger.info("DomeClient disconnecting...")
        self._running = False
        self._fail_pending(RuntimeError("Dome client disconnected"))
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:
                logger.error("Error closing WebSocket: %s", e)
        if self._task is not None:
            self._task.cancel()
        self._ws = None
        self._connected = False
        logger.info("DomeClient disconnected")

    async def wait_for_telemetry(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._telemetry_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _authenticate(self, ws) -> None:
        if not self.access_key:
            raise DomeAuthError("Не задан ключ доступа DOME_ACCESS_KEY")
        await ws.send(json.dumps({"type": "auth", "key": self.access_key}))
        response = json.loads(await asyncio.wait_for(ws.recv(), timeout=self._response_timeout))
        if response.get("type") != "auth_response" or not response.get("success"):
            raise DomeAuthError(response.get("error") or "Авторизация отклонена")
        self._role = response.get("role")
        self._auth_error = None
        logger.info("✅ Authorized on dome server as %s", self._role)

    async def _connection_loop(self) -> None:
        while self._running:
            delay = self.reconnect_delay
            try:
                logger.info("Connecting to dome server at %s...", self.url)
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=10,
                                              max_size=10_000_000) as ws:
                    await self._authenticate(ws)
                    self._ws = ws
                    self._connected = True
                    asyncio.create_task(self._refresh_catalog())
                    async for message in ws:
                        await self._handle_message(message)
            except asyncio.CancelledError:
                break
            except DomeAuthError as e:
                self._auth_error = str(e)
                logger.error("❌ Dome server rejected access: %s", e)
                delay = AUTH_RETRY_DELAY
            except Exception as e:
                logger.error("Connection error: %s", e)
            finally:
                self._connected = False
                self._ws = None
                self._fail_pending(RuntimeError("Связь с куполом потеряна"))
            if self._running:
                logger.info("Reconnecting in %.1f seconds...", delay)
                await asyncio.sleep(delay)

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    # ------------------------------------------------------------------
    # Входящие сообщения
    # ------------------------------------------------------------------

    async def _handle_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse message: %s", e)
            return

        msg_type = data.get("type")
        if msg_type == "telemetry":
            payload = data.get("data", {})
            if isinstance(payload, dict):
                self._latest_telemetry = {**payload, "timestamp": data.get("timestamp")}
                self._telemetry_event.set()
        elif data.get("request_id") in self._pending:
            future = self._pending.pop(data["request_id"])
            if not future.done():
                future.set_result(data)
        elif msg_type == "session_revoked":
            logger.warning("Dome server revoked session: %s", data.get("reason"))
        else:
            logger.debug("Unhandled message: %s", msg_type)

    # ------------------------------------------------------------------
    # Запросы
    # ------------------------------------------------------------------

    async def request(self, msg_type: str, **fields: Any) -> dict[str, Any]:
        """Отправляет запрос и ждёт ответ с тем же request_id."""
        if not self.is_connected():
            raise RuntimeError(self._auth_error or "Нет подключения к серверу купола")
        request_id = f"qa{next(self._request_ids)}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._ws.send(json.dumps({"type": msg_type, "request_id": request_id, **fields},
                                           ensure_ascii=False))
            return await asyncio.wait_for(future, timeout=self._response_timeout)
        finally:
            self._pending.pop(request_id, None)

    async def send_control_command(self, node_id: str, action: str, value: Any = None) -> dict[str, Any]:
        """
        Управляющая команда узлу купола.

        Returns:
            Ответ сервера: success, node_id, action, mode (real/virtual) или error.
        """
        if not self.enable_control:
            raise RuntimeError("Dome control is disabled in configuration")
        fields: dict[str, Any] = {"node_id": node_id, "action": action}
        if value is not None:
            fields["value"] = value
        response = await self.request("control", **fields)
        logger.info("Control %s.%s(%r) -> %s", node_id, action, value,
                    "ok" if response.get("success") else response.get("error"))
        return response

    async def _refresh_catalog(self) -> None:
        try:
            response = await self.request("describe_nodes")
            if response.get("success"):
                self._catalog = {n["node_id"]: n for n in response.get("nodes", [])}
                logger.info("Dome catalog loaded: %d nodes", len(self._catalog))
        except Exception as e:
            logger.warning("Failed to load dome catalog: %s", e)

    # ------------------------------------------------------------------
    # Данные
    # ------------------------------------------------------------------

    def get_latest_telemetry(self) -> dict[str, Any] | None:
        """Последняя телеметрия: nodes, game_time (+ для админа node_modes, active_crises, ...)."""
        return self._latest_telemetry

    def get_catalog(self) -> dict[str, dict[str, Any]]:
        """Каталог узлов: node_id -> {title, system, controls, ...}."""
        return self._catalog

    def get_key_nodes(self) -> dict[str, Any] | None:
        """Только ключевые узлы (для промпта Хранителя)."""
        if not self._latest_telemetry:
            return None
        all_nodes = self._latest_telemetry.get("nodes", {})
        return {
            "nodes": {nid: all_nodes[nid] for nid in self.KEY_NODES if nid in all_nodes},
            "active_crises": self._latest_telemetry.get("active_crises", []),
            "game_time": self._latest_telemetry.get("game_time"),
            "timestamp": self._latest_telemetry.get("timestamp", 0.0),
            "connected": self._connected,
        }

    def get_node_status(self, node_id: str) -> dict[str, Any] | None:
        if not self._latest_telemetry:
            return None
        return self._latest_telemetry.get("nodes", {}).get(node_id)

    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    def get_connection_info(self) -> dict[str, Any]:
        ts = None
        if self._latest_telemetry and self._latest_telemetry.get("timestamp"):
            try:
                ts = datetime.fromtimestamp(self._latest_telemetry["timestamp"]).isoformat()
            except (TypeError, ValueError, OSError):
                ts = str(self._latest_telemetry.get("timestamp"))
        return {
            "url": self.url,
            "connected": self._connected,
            "role": self._role,
            "auth_error": self._auth_error,
            "running": self._running,
            "enable_control": self.enable_control,
            "last_telemetry": ts,
        }


# Глобальный экземпляр клиента (используется приложением)
dome_client = DomeClient()


# ---------------------------------------------------------------------------
# Автономный тест: python -m app.dome_client (нужен DOME_ACCESS_KEY)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    async def run_test_scenario() -> None:
        client = DomeClient(response_timeout=5.0, enable_control=True)
        await client.connect()
        if not await client.wait_for_telemetry(timeout=10.0):
            print("⚠️ Телеметрия не пришла:", client.get_connection_info())
            await client.disconnect()
            return
        key = client.get_key_nodes()
        print(f"✅ Телеметрия: {len(key['nodes'])} ключевых узлов, игровое время {key['game_time']}")
        await asyncio.sleep(1.0)
        print(f"Каталог: {len(client.get_catalog())} узлов")
        for node_id, action, value in [("smart_panel_01", "line_on", 1), ("dizel_1", "turn_on", None)]:
            response = await client.send_control_command(node_id, action, value)
            print(f"{node_id}.{action}: {response.get('success')} {response.get('error', '')}")
        await client.disconnect()

    try:
        asyncio.run(run_test_scenario())
    except KeyboardInterrupt:
        print("\nКлиент остановлен.")

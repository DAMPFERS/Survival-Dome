"""WebSocket-мост между синхронным Python-оркестратором и браузером."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict, Optional, Set

import websockets

from chat_bus import ChatMessage, SharedChatLog
from dashboard import compute_snapshot
from orchestrator import Orchestrator
from tool_registry import ToolExecutor

logger = logging.getLogger(__name__)
DASHBOARD_INTERVAL_SEC = 4.0
MAX_OPERATOR_MESSAGE_LENGTH = 2000


def _ui_type_for(message: ChatMessage, coordinator_name: str) -> str:
    if message.is_event:
        return "danger"
    if message.speaker in {"Оператор", "Система"}:
        return "system"
    if message.speaker == coordinator_name:
        return "decision"
    return "agent"


def _chat_message_to_wire(message: ChatMessage, coordinator_name: str) -> Dict[str, Any]:
    return {
        "id": message.message_id,
        "rootId": message.root_id,
        "depth": message.depth,
        "t": message.timestamp,
        "type": _ui_type_for(message, coordinator_name),
        "agent": message.speaker,
        "text": message.content,
    }


class WebBridge:
    def __init__(
        self,
        bus: SharedChatLog,
        orchestrator: Orchestrator,
        tool_executor: ToolExecutor,
        coordinator_name: str,
        host: str = "0.0.0.0",
        port: int = 8766,
    ):
        self._bus = bus
        self._orchestrator = orchestrator
        self._tools = tool_executor
        self._coordinator_name = coordinator_name
        self._host = host
        self._port = port

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients: Set[Any] = set()
        self._thread: Optional[threading.Thread] = None
        self._last_snapshot: Dict[str, Any] = {}
        self._ready = threading.Event()
        self._stop_async: Optional[asyncio.Event] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="WebBridge")
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise TimeoutError("WebBridge не запустился за 5 секунд")
        self._bus.subscribe(self._on_new_chat_message)

    def stop(self) -> None:
        self._bus.unsubscribe(self._on_new_chat_message)
        if self._loop and self._stop_async:
            self._loop.call_soon_threadsafe(self._stop_async.set)
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _main(self) -> None:
        self._stop_async = asyncio.Event()
        async with websockets.serve(
            self._handle_client,
            self._host,
            self._port,
            ping_interval=20,
            ping_timeout=20,
            max_size=64 * 1024,
        ):
            logger.info("WebBridge слушает ws://%s:%d", self._host, self._port)
            self._ready.set()
            dashboard_task = asyncio.create_task(self._dashboard_loop())
            await self._stop_async.wait()
            dashboard_task.cancel()
            await asyncio.gather(dashboard_task, return_exceptions=True)
            await self._close_clients()

    async def _handle_client(self, websocket: Any) -> None:
        self._clients.add(websocket)
        logger.info("Браузер подключился (%d)", len(self._clients))
        try:
            await self._send_history(websocket)
            async for raw in websocket:
                await self._handle_incoming(websocket, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)
            logger.info("Браузер отключился (%d)", len(self._clients))

    async def _send_history(self, websocket: Any) -> None:
        history = [
            _chat_message_to_wire(message, self._coordinator_name)
            for message in self._bus.since(0)
        ]
        snapshot = self._last_snapshot or await asyncio.to_thread(compute_snapshot, self._tools)
        await websocket.send(json.dumps({
            "type": "history",
            "chat": history,
            "dashboard": snapshot,
        }, ensure_ascii=False))

    async def _handle_incoming(self, websocket: Any, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self._send_error(websocket, "Некорректный JSON")
            return

        if data.get("type") != "operator_message":
            await self._send_error(websocket, "Неизвестный тип сообщения")
            return

        text = str(data.get("text", "")).strip()
        if not text:
            return
        if len(text) > MAX_OPERATOR_MESSAGE_LENGTH:
            await self._send_error(websocket, "Сообщение слишком длинное")
            return
        self._orchestrator.submit_operator_message(text)

    async def _send_error(self, websocket: Any, text: str) -> None:
        await websocket.send(json.dumps({"type": "error", "text": text}, ensure_ascii=False))

    def _on_new_chat_message(self, message: ChatMessage) -> None:
        if not self._loop or self._loop.is_closed():
            return
        payload = json.dumps({
            "type": "chat",
            "message": _chat_message_to_wire(message, self._coordinator_name),
        }, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(self._broadcast(payload), self._loop)

    async def _dashboard_loop(self) -> None:
        while True:
            try:
                snapshot = await asyncio.to_thread(compute_snapshot, self._tools)
                self._last_snapshot = snapshot
                await self._broadcast(json.dumps({
                    "type": "dashboard",
                    "params": snapshot,
                }, ensure_ascii=False))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка обновления дашборда")
            await asyncio.sleep(DASHBOARD_INTERVAL_SEC)

    async def _broadcast(self, payload: str) -> None:
        if not self._clients:
            return
        await asyncio.gather(
            *(self._safe_send(client, payload) for client in list(self._clients)),
            return_exceptions=True,
        )

    async def _safe_send(self, websocket: Any, payload: str) -> None:
        try:
            await websocket.send(payload)
        except websockets.ConnectionClosed:
            self._clients.discard(websocket)

    async def _close_clients(self) -> None:
        await asyncio.gather(
            *(client.close(code=1001, reason="Server shutdown") for client in list(self._clients)),
            return_exceptions=True,
        )
        self._clients.clear()

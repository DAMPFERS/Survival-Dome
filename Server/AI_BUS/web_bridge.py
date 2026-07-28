"""
Веб-мост между Python-оркестратором (синхронный, threading) и браузером
(асинхронный WebSocket, отдельный event loop в своём потоке).

Задачи моста:
1. При подключении новой вкладки — отдаёт полную историю чата и текущий
   снимок телеметрии, чтобы вкладка сразу увидела актуальную картину, а не
   стартовала с пустого места.
2. Транслирует каждое новое сообщение из SharedChatLog всем подключённым
   браузерам в реальном времени (через хук SharedChatLog.on_message).
3. Раз в DASHBOARD_INTERVAL_SEC считает снимок телеметрии (dashboard.py) и
   рассылает его всем подключённым браузерам.
4. Принимает сообщения оператора из браузера и передаёт их в Orchestrator —
   дальше они идут по общей логике (шина чата, релевантность, агенты).

Оркестратор и CrisisEngine работают в основном потоке синхронно, как и
раньше; мост поднимает СВОЙ независимый asyncio event loop в отдельном
потоке, чтобы не блокировать и не блокироваться синхронным кодом.
"""

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


def _ui_type_for(msg: ChatMessage, coordinator_name: str) -> str:
    """Определяет визуальный тип сообщения (цвет плашки в чате) по автору/флагу события."""
    if msg.is_event:
        return "danger"
    if msg.speaker == "Оператор":
        return "system"
    if msg.speaker == coordinator_name:
        return "decision"
    return "agent"


def _chat_message_to_wire(msg: ChatMessage, coordinator_name: str) -> Dict[str, Any]:
    return {
        "t": msg.timestamp,
        "type": _ui_type_for(msg, coordinator_name),
        "agent": msg.speaker,
        "text": msg.content,
    }


class WebBridge:
    def __init__(self, bus: SharedChatLog, orchestrator: Orchestrator,
                 tool_executor: ToolExecutor, coordinator_name: str,
                 host: str = "0.0.0.0", port: int = 8766):
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

    # --- запуск/остановка ---

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        # Подписываемся на новые сообщения общего чата. Вызывается СИНХРОННО
        # из потока Orchestrator сразу после bus.append(...).
        self._bus.on_message = self._on_new_chat_message

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        async with websockets.serve(self._handle_client, self._host, self._port):
            logger.info("WebBridge слушает ws://%s:%d", self._host, self._port)
            self._ready.set()
            asyncio.create_task(self._dashboard_loop())
            await asyncio.Future()  # держим цикл живым, пока stop() его не остановит

    # --- обработка подключений браузера ---

    async def _handle_client(self, ws: Any) -> None:
        self._clients.add(ws)
        logger.info("Браузер подключился (%d активных)", len(self._clients))
        try:
            await self._send_history(ws)
            async for raw in ws:
                await self._handle_incoming(raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            logger.info("Браузер отключился (%d активных)", len(self._clients))

    async def _send_history(self, ws: Any) -> None:
        history = [
            _chat_message_to_wire(m, self._coordinator_name)
            for m in self._bus.since(0)
        ]
        snapshot = self._last_snapshot or await asyncio.to_thread(compute_snapshot, self._tools)
        payload = json.dumps(
            {"type": "history", "chat": history, "dashboard": snapshot}, ensure_ascii=False
        )
        await ws.send(payload)

    async def _handle_incoming(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Некорректный JSON от браузера: %r", raw[:200])
            return
        if data.get("type") == "operator_message":
            text = str(data.get("text", "")).strip()
            if text:
                self._orchestrator.submit_operator_message(text)

    # --- рассылка ---

    def _on_new_chat_message(self, msg: ChatMessage) -> None:
        """Колбэк из потока Orchestrator (синхронный вызов) — планируем рассылку в его loop."""
        if not self._loop:
            return
        wire = _chat_message_to_wire(msg, self._coordinator_name)
        payload = json.dumps({"type": "chat", "message": wire}, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(self._broadcast(payload), self._loop)

    async def _dashboard_loop(self) -> None:
        while True:
            snapshot = await asyncio.to_thread(compute_snapshot, self._tools)
            self._last_snapshot = snapshot
            payload = json.dumps({"type": "dashboard", "params": snapshot}, ensure_ascii=False)
            await self._broadcast(payload)
            await asyncio.sleep(DASHBOARD_INTERVAL_SEC)

    async def _broadcast(self, payload: str) -> None:
        if not self._clients:
            return
        await asyncio.gather(
            *(self._safe_send(ws, payload) for ws in list(self._clients)),
            return_exceptions=True,
        )

    async def _safe_send(self, ws: Any, payload: str) -> None:
        try:
            await ws.send(payload)
        except websockets.ConnectionClosed:
            pass
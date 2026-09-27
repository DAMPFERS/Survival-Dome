#!/usr/bin/env python3
"""
Админ-панель купола.

Скрипт подключается к серверу купола (server_integrated.py) по WebSocket с
админским ключом и поднимает локальную веб-страницу. Браузер общается только
со скриптом (ws://<host>:<port>/ws): скрипт пересылает ему телеметрию сервера,
а запросы браузера — серверу, и возвращает ответы. Админский ключ в браузер
не передаётся.

Запуск:
    python admin_panel.py                                  # ключ из data/Hash-keys/admin.txt
    python admin_panel.py --server ws://192.168.1.10:8765 --port 8080
    python admin_panel.py --key <ключ>                     # или переменная DOME_ADMIN_KEY

Затем откройте http://127.0.0.1:8080 в браузере.
По умолчанию страница доступна только с этого компьютера (--host 127.0.0.1):
через неё выполняются любые админские команды. --host 0.0.0.0 открывает её в сети.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import mimetypes
import os
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

ADMIN_DIR = Path(__file__).resolve().parent
STATIC_DIR = ADMIN_DIR / "static"
DEFAULT_KEY_FILE = ADMIN_DIR.parent.parent / "data" / "Hash-keys" / "admin.txt"
RECONNECT_S = 3.0
AUTH_RETRY_S = 15.0
MAX_MESSAGE = 50_000_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("admin_panel")
logging.getLogger("websockets").setLevel(logging.WARNING)  # без строк на каждый HTTP-запрос статики


class Bridge:
    """Мост «сервер купола ↔ браузеры админ-панели»."""

    def __init__(self, server_uri: str, key: str) -> None:
        self.server_uri = server_uri
        self.key = key
        self.dome: Optional[Any] = None
        self.browsers: set[ServerConnection] = set()
        self.status: dict[str, Any] = {"type": "bridge_status", "connected": False, "server": server_uri,
                                       "role": None, "error": None}
        self.last_telemetry: Optional[str] = None
        self.pending: dict[str, tuple[ServerConnection, Any]] = {}
        self._ids = itertools.count(1)

    # ---------------- сервер купола ----------------

    async def dome_loop(self) -> None:
        while True:
            delay = RECONNECT_S
            try:
                async with websockets.connect(self.server_uri, max_size=MAX_MESSAGE, ping_interval=20,
                                              close_timeout=3) as ws:
                    await ws.send(json.dumps({"type": "auth", "key": self.key, "request_id": "auth"}))
                    answer = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if not answer.get("success"):
                        await self.set_status(False, error=f"Авторизация отклонена: {answer.get('error')}")
                        delay = AUTH_RETRY_S
                        continue
                    if answer.get("role") != "admin":
                        await self.set_status(False, error="Ключ не админский — нужен ключ администратора")
                        delay = AUTH_RETRY_S
                        continue
                    self.dome = ws
                    await self.set_status(True, role=answer.get("role"))
                    logger.info("Подключено к %s как admin", self.server_uri)
                    async for raw in ws:
                        await self.on_dome_message(raw)
            except (OSError, ConnectionClosed, asyncio.TimeoutError, json.JSONDecodeError) as e:
                if self.status["connected"] or not self.status["error"]:
                    logger.warning("Нет связи с сервером купола: %s", e)
                await self.set_status(False, error=f"Нет связи с сервером купола ({type(e).__name__})")
            finally:
                self.dome = None
                await self.fail_pending("Соединение с сервером купола потеряно")
            await asyncio.sleep(delay)

    async def on_dome_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if data.get("type") == "telemetry":
            self.last_telemetry = raw
            await self.broadcast(raw)
            return
        route = self.pending.pop(str(data.get("request_id")), None)
        if route is not None:
            browser, original_id = route
            data["request_id"] = original_id
            await self.send(browser, json.dumps(data, ensure_ascii=False))
        else:
            await self.broadcast(raw)  # сообщения сервера без запроса (session_revoked и т.п.)

    async def set_status(self, connected: bool, role: Optional[str] = None, error: Optional[str] = None) -> None:
        self.status.update(connected=connected, role=role if connected else None, error=error)
        await self.broadcast(json.dumps(self.status, ensure_ascii=False))

    async def fail_pending(self, reason: str) -> None:
        pending, self.pending = self.pending, {}
        for browser, original_id in pending.values():
            await self.send(browser, json.dumps({"type": "error", "request_id": original_id,
                                                 "success": False, "error": reason}, ensure_ascii=False))

    # ---------------- браузеры ----------------

    async def browser_handler(self, ws: ServerConnection) -> None:
        self.browsers.add(ws)
        logger.info("Браузер подключён (%d)", len(self.browsers))
        try:
            await self.send(ws, json.dumps(self.status, ensure_ascii=False))
            if self.last_telemetry:
                await self.send(ws, self.last_telemetry)
            async for raw in ws:
                await self.on_browser_message(ws, raw)
        except ConnectionClosed:
            pass
        finally:
            self.browsers.discard(ws)
            logger.info("Браузер отключён (%d)", len(self.browsers))

    async def on_browser_message(self, ws: ServerConnection, raw: str) -> None:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or not data.get("type"):
                raise ValueError
        except ValueError:
            await self.send(ws, json.dumps({"type": "error", "success": False, "error": "Некорректный запрос"}))
            return
        original_id = data.get("request_id")
        if data["type"] == "auth":
            return  # авторизацию выполняет только мост
        if self.dome is None:
            await self.send(ws, json.dumps({"type": f"{data['type']}_response", "request_id": original_id,
                                            "success": False, "error": "Нет связи с сервером купола"},
                                           ensure_ascii=False))
            return
        rid = f"panel-{next(self._ids)}"
        self.pending[rid] = (ws, original_id)
        data["request_id"] = rid
        try:
            await self.dome.send(json.dumps(data, ensure_ascii=False))
        except ConnectionClosed:
            self.pending.pop(rid, None)
            await self.send(ws, json.dumps({"type": "error", "request_id": original_id, "success": False,
                                            "error": "Соединение с сервером купола потеряно"}))

    async def send(self, ws: ServerConnection, text: str) -> None:
        try:
            await ws.send(text)
        except ConnectionClosed:
            self.browsers.discard(ws)

    async def broadcast(self, text: str) -> None:
        for ws in list(self.browsers):
            await self.send(ws, text)


# ---------------------------------------------------------------------------
# Статика (HTTP на том же порту, что и WebSocket /ws)
# ---------------------------------------------------------------------------

def static_response(connection: ServerConnection, request: Request) -> Optional[Response]:
    path = unquote(urlsplit(request.path).path)
    if path == "/ws":
        return None  # WebSocket-рукопожатие
    if path in ("", "/"):
        path = "/index.html"
    target = (STATIC_DIR / path.lstrip("/")).resolve()
    if STATIC_DIR not in target.parents or not target.is_file():
        return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
    body = target.read_bytes()
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript",):
        ctype += "; charset=utf-8"
    headers = Headers([("Content-Type", ctype), ("Content-Length", str(len(body))),
                       ("Cache-Control", "no-cache")])
    return Response(HTTPStatus.OK.value, "OK", headers, body)


def read_key(args: argparse.Namespace) -> str:
    if args.key:
        return args.key.strip()
    if os.getenv("DOME_ADMIN_KEY"):
        return os.environ["DOME_ADMIN_KEY"].strip()
    key_file = Path(args.key_file)
    if key_file.is_file():
        return key_file.read_text(encoding="utf-8").strip()
    raise SystemExit(f"Нет админского ключа: --key, DOME_ADMIN_KEY или файл {key_file}")


async def main(args: argparse.Namespace) -> None:
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    bridge = Bridge(args.server, read_key(args))
    dome_task = asyncio.create_task(bridge.dome_loop())
    async with serve(bridge.browser_handler, args.host, args.port, process_request=static_response,
                     max_size=MAX_MESSAGE):
        logger.info("Админ-панель: http://%s:%d  (сервер купола: %s)",
                    "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host, args.port, args.server)
        try:
            await asyncio.Future()
        finally:
            dome_task.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Веб-панель администратора купола")
    parser.add_argument("--server", default=os.getenv("DOME_SERVER", "ws://localhost:8765"),
                        help="адрес сервера купола (ws://host:port)")
    parser.add_argument("--host", default=os.getenv("ADMIN_HOST", "127.0.0.1"), help="адрес веб-страницы")
    parser.add_argument("--port", type=int, default=int(os.getenv("ADMIN_PORT", "8080")))
    parser.add_argument("--key", help="админский ключ (иначе DOME_ADMIN_KEY или --key-file)")
    parser.add_argument("--key-file", default=str(DEFAULT_KEY_FILE))
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        print("\nАдмин-панель остановлена.")
        sys.exit(0)

#!/usr/bin/env python3
"""
Маршрутизатор куполов: один внешний порт, несколько независимых куполов за ним.

Каждая команда работает в своём куполе (отдельный процесс server_integrated.py,
активная команда = номер купола, слушает внутренний порт). Участники подключаются
к одному адресу ws://<host>:8765 — маршрутизатор читает первое сообщение
{"type": "auth", "key": ...}, по ключу определяет команду и дальше прозрачно
пересылает трафик в обе стороны в её купол (первое сообщение тоже — купол сам
проверяет ключ и отвечает auth_response).

Админский ключ подходит ко всем куполам, поэтому купол админ выбирает путём в URL:
    ws://<host>:8765/team/2      (или /2)
    python admin/admin_panel.py --server ws://<host>:8765/team/2 --port 8082
Для ключа команды путь не нужен и не учитывается: купол определяет ключ.

Настройка (переменные окружения):
    DOME_ROUTER_HOST    — адрес маршрутизатора (по умолчанию 0.0.0.0)
    DOME_ROUTER_PORT    — внешний порт (по умолчанию 8765)
    DOME_ROUTES         — купола: "1=ws://127.0.0.1:18765,2=ws://127.0.0.1:18766,..."
                          (по умолчанию 4 купола на 127.0.0.1:18765..18768)
    DOME_KEYS_CONFIG    — хэши ключей (authorization/keys_config.json)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any, Optional

from websockets import connect, serve
from websockets.exceptions import ConnectionClosed, InvalidHandshake

SERVER_DIR = Path(__file__).parent
sys.path.insert(0, str(SERVER_DIR))

from authorization.protector import Protector  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("websockets").setLevel(logging.WARNING)
logger = logging.getLogger("DomeRouter")

HOST = os.getenv("DOME_ROUTER_HOST", "0.0.0.0")
PORT = int(os.getenv("DOME_ROUTER_PORT", "8765"))
AUTH_TIMEOUT = float(os.getenv("DOME_AUTH_TIMEOUT", "10.0"))
MAX_MESSAGE = int(os.getenv("DOME_MAX_MESSAGE", str(32 * 1024 * 1024)))
KEYS_CONFIG = os.getenv("DOME_KEYS_CONFIG", str(SERVER_DIR / "authorization" / "keys_config.json"))
BACKEND_BASE_PORT = int(os.getenv("DOME_BACKEND_BASE_PORT", "18765"))

CLOSE_UNAUTHORIZED = 4401
CLOSE_TRY_AGAIN = 1013   # купол недоступен

PATH_TEAM = re.compile(r"^/(?:team/?)?([1-4])/?$")


def parse_routes(raw: str) -> dict[int, str]:
    """"1=ws://127.0.0.1:18765,2=..." -> {1: "ws://127.0.0.1:18765", ...}"""
    routes: dict[int, str] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        team, sep, uri = item.partition("=")
        if not sep or not team.strip().isdigit() or not uri.strip():
            raise ValueError(f"DOME_ROUTES: ожидалось N=ws://host:port, получено {item!r}")
        routes[int(team)] = uri.strip()
    return routes


def default_routes() -> dict[int, str]:
    return {team: f"ws://127.0.0.1:{BACKEND_BASE_PORT + team - 1}" for team in (1, 2, 3, 4)}


def reject_plain_http(connection, request):
    """Обычный HTTP без апгрейда (сканеры портов, браузер, проверки живости) — короткий
    ответ 426 вместо трассировки «opening handshake failed» в логе."""
    tokens = {t.strip().lower() for v in request.headers.get_all("Connection") for t in v.split(",")}
    if "upgrade" not in tokens or request.headers.get("Upgrade", "").lower() != "websocket":
        return connection.respond(HTTPStatus.UPGRADE_REQUIRED, "Survival Dome: WebSocket only\n")
    return None


class DomeRouter:
    def __init__(self, routes: dict[int, str]) -> None:
        self.routes = routes
        self.protector = Protector(KEYS_CONFIG)

    def _team_for(self, key: Any, path_team: Optional[int]) -> tuple[Optional[int], Optional[str]]:
        """(купол, ошибка). Ключ команды ведёт в её купол, админский — в купол из URL."""
        index = self.protector.getTeamIndex(key)
        if index is not None:
            return index + 1, None
        if self.protector.getRole(key) == "admin":   # без активной команды getRole видит только админа
            if path_team is None:
                return None, (f"Админский ключ: укажите купол в адресе, например "
                              f"ws://<host>:{PORT}/team/1 (купола: {sorted(self.routes)})")
            return path_team, None
        return None, "Неверный ключ доступа"

    @staticmethod
    async def _reject(websocket, request_id: Any, error: str, code: int, reason: str) -> None:
        try:
            await websocket.send(json.dumps({"type": "auth_response", "request_id": request_id,
                                             "success": False, "error": error}, ensure_ascii=False))
            await websocket.close(code, reason)
        except ConnectionClosed:
            pass

    async def handle_client(self, websocket) -> None:
        peer = websocket.remote_address
        match = PATH_TEAM.match(websocket.request.path.split("?", 1)[0])
        path_team = int(match.group(1)) if match else None

        try:
            first = await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            await websocket.close(CLOSE_UNAUTHORIZED, "auth timeout")
            return
        except ConnectionClosed:
            return
        try:
            data = json.loads(first)
        except (json.JSONDecodeError, TypeError):
            data = None
        if not isinstance(data, dict):
            data = {}
        request_id = data.get("request_id")

        team, error = (None, "Первое сообщение — {\"type\": \"auth\", \"key\": ...}") \
            if data.get("type") != "auth" else self._team_for(data.get("key"), path_team)
        if team is not None and team not in self.routes:
            team, error = None, f"Купол {team} не запущен (купола: {sorted(self.routes)})"
        if team is None:
            logger.warning("Отказ %s (path=%s): %s", peer, websocket.request.path, error)
            await asyncio.sleep(1.0)  # замедляем перебор ключей, как сам сервер
            await self._reject(websocket, request_id, error, CLOSE_UNAUTHORIZED, "unauthorized")
            return

        uri = self.routes[team]
        try:
            backend = await connect(uri, max_size=MAX_MESSAGE, open_timeout=5)
        except (OSError, InvalidHandshake, asyncio.TimeoutError) as e:
            logger.error("Купол %d (%s) недоступен для %s: %s", team, uri, peer, e)
            await self._reject(websocket, request_id, f"Купол {team} сейчас недоступен — повторите позже",
                               CLOSE_TRY_AGAIN, "dome unavailable")
            return

        logger.info("%s -> купол %d (%s)", peer, team, uri)
        try:
            await backend.send(first)
            await self._pipe(websocket, backend)
        finally:
            await backend.close()
            logger.info("%s отключён от купола %d", peer, team)

    @staticmethod
    async def _pipe(client, backend) -> None:
        """Пересылает сообщения в обе стороны; закрытие одной стороны (с её кодом) закрывает другую."""

        async def forward(src, dst) -> None:
            try:
                async for message in src:
                    await dst.send(message)
            except ConnectionClosed:
                pass
            # код закрытия купола (4401, 4403 — сессия отозвана) доходит до клиента
            code = src.close_code if src.close_code not in (None, 1005, 1006) else 1000
            await dst.close(code, src.close_reason or "")

        tasks = [asyncio.create_task(forward(client, backend)), asyncio.create_task(forward(backend, client))]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def main() -> None:
    raw = os.getenv("DOME_ROUTES", "").strip()
    try:
        routes = parse_routes(raw) if raw else default_routes()
        router = DomeRouter(routes)
    except (FileNotFoundError, ValueError) as e:
        logger.error("%s", e)
        return
    async with serve(router.handle_client, HOST, PORT, max_size=MAX_MESSAGE,
                     process_request=reject_plain_http):
        logger.info("Маршрутизатор куполов на ws://%s:%d", HOST, PORT)
        for team, uri in sorted(routes.items()):
            logger.info("  команда %d -> %s (админ: ws://<host>:%d/team/%d)", team, uri, PORT, team)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

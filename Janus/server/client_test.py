"""
Тестовый клиент сервера купола (server_integrated.py).

Сценарий:
    1. Авторизация ключом (админ или активная команда).
    2. Первая телеметрия и каталог узлов (describe_nodes) — с учётом роли.
    3. Разрешённые участнику команды: включение линии щитка, запуск дизеля.
    4. Команда только для админа (эмуляция срабатывания защиты) — участнику
       должен прийти отказ.
    5. Админские запросы (time_control, switch_mode) — участнику отказ.

Запуск:
    python client_test.py --key <ключ>            # ключ можно задать и в DOME_KEY
    python client_test.py --key <ключ> --verbose  # печатать телеметрию целиком
"""
import argparse
import asyncio
import itertools
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

COMMAND_TIMEOUT = 5.0
TELEMETRY_WAIT = 8.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class DomeTestClient:
    def __init__(self, websocket, verbose: bool = False) -> None:
        self.ws = websocket
        self.verbose = verbose
        self.role: str | None = None
        self.telemetry: dict[str, Any] = {}
        self.telemetry_received = asyncio.Event()
        self._waiters: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)

    # ---------------- приём ----------------

    async def receive_loop(self) -> None:
        async for raw in self.ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Невалидный JSON: %s", raw)
                continue
            msg_type = data.get("type", "")
            if msg_type == "telemetry":
                self.telemetry = data
                self.print_telemetry(data, full=self.verbose or not self.telemetry_received.is_set())
                self.telemetry_received.set()
            elif data.get("request_id") in self._waiters:
                future = self._waiters.pop(data["request_id"])
                if not future.done():
                    future.set_result(data)
            else:
                print("\nSERVER MESSAGE:", json.dumps(data, ensure_ascii=False, indent=2))

    @staticmethod
    def print_telemetry(data: dict[str, Any], full: bool) -> None:
        payload = data.get("data", {})
        nodes = payload.get("nodes", {})
        stamp = datetime.fromtimestamp(data.get("timestamp", 0)).strftime("%H:%M:%S")
        game = payload.get("game_time", {})
        print(f"\n📡 TELEMETRY {stamp} role={data.get('role')} nodes={len(nodes)} "
              f"game=день {game.get('day')}, {game.get('hour')} ч")
        if not full:
            return
        extra = {k: v for k, v in payload.items()
                 if k not in ("nodes", "environment", "real_devices", "game_time")}
        print("   поля:", json.dumps(extra, ensure_ascii=False))
        modes = payload.get("node_modes", {})
        for node_id, params in nodes.items():
            mode = modes.get(node_id, "-")
            print(f"   {node_id:<24} mode={mode:<7} {json.dumps(params, ensure_ascii=False)[:180]}")

    # ---------------- запросы ----------------

    async def request(self, data: dict[str, Any], quiet: bool = False) -> dict[str, Any] | None:
        request_id = f"t{next(self._ids)}"
        data = {**data, "request_id": request_id}
        future = asyncio.get_running_loop().create_future()
        self._waiters[request_id] = future
        if not quiet:
            print("\n" + "-" * 70)
            print("📤 CLIENT -> SERVER:", json.dumps(data, ensure_ascii=False))
        await self.ws.send(json.dumps(data, ensure_ascii=False))
        try:
            response = await asyncio.wait_for(future, timeout=COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            self._waiters.pop(request_id, None)
            print(f"⏱ Нет ответа сервера за {COMMAND_TIMEOUT} с")
            return None
        if not quiet:
            print("📥 SERVER -> CLIENT:", json.dumps(response, ensure_ascii=False))
        return response

    async def authenticate(self, key: str) -> bool:
        """Авторизация — до запуска receive_loop: ответ читаем напрямую."""
        await self.ws.send(json.dumps({"type": "auth", "key": key, "request_id": "auth"}))
        response = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=COMMAND_TIMEOUT + 2))
        if not response.get("success"):
            print(f"❌ Авторизация отклонена: {response.get('error')}")
            return False
        self.role = response["role"]
        team = f", команда {response['team']}" if response.get("team") else ""
        print(f"✅ Авторизован: роль {self.role}{team}")
        return True


results: list[bool] = []


def check(title: str, response: dict[str, Any] | None, expect_success: bool) -> None:
    ok = bool(response) and response.get("success") == expect_success
    results.append(ok)
    expectation = "успех" if expect_success else "отказ"
    detail = "" if ok or not response else f", получено: {response.get('error') or 'успех'}"
    print(f"{'✅' if ok else '❌'} {title}: ожидался {expectation}{detail}")


async def run_scenario(client: DomeTestClient) -> None:
    is_admin = client.role == "admin"

    print("\n⏳ Ожидание первой телеметрии...")
    try:
        await asyncio.wait_for(client.telemetry_received.wait(), timeout=TELEMETRY_WAIT)
    except asyncio.TimeoutError:
        print("⚠️ Телеметрия не пришла вовремя")

    print("\n" + "#" * 70 + "\n# Каталог узлов\n" + "#" * 70)
    catalog = await client.request({"type": "describe_nodes"}, quiet=True)
    for node in (catalog or {}).get("nodes", []):
        mode = f" [{node['mode']}]" if "mode" in node else ""
        print(f"  {node['node_id']:<24}{mode:<10} {node['title']}: {', '.join(node['controls']) or '—'}")

    print("\n" + "#" * 70 + "\n# Разрешённые участнику команды\n" + "#" * 70)
    check("Включение линии 1 щитка",
          await client.request({"type": "control", "node_id": "smart_panel_01", "action": "line_on", "value": 1}),
          expect_success=True)
    check("Запуск дизель-генератора",
          await client.request({"type": "control", "node_id": "dizel_1", "action": "turn_on"}),
          expect_success=True)
    check("Недопустимое значение (линия 42)",
          await client.request({"type": "control", "node_id": "smart_panel_01", "action": "line_on", "value": 42}),
          expect_success=False)

    print("\n" + "#" * 70 + "\n# Команды и запросы только для админа\n" + "#" * 70)
    check("Эмуляция срабатывания защиты линии 8",
          await client.request({"type": "control", "node_id": "smart_panel_01",
                                "action": "emulate_protection_trip", "value": {"line": 8}}),
          expect_success=is_admin)
    panel = client.telemetry.get("data", {}).get("nodes", {}).get("smart_panel_01", {})
    check("Служебные параметры (control_*) видны только админу",
          {"success": is_admin == ("control_override_source" in panel)}, expect_success=True)
    check("Ускорение времени x2",
          await client.request({"type": "time_control", "action": "set_scale", "value": 2.0}),
          expect_success=is_admin)
    if is_admin:
        await client.request({"type": "time_control", "action": "set_scale", "value": 1.0})
        response = await client.request({"type": "switch_mode", "node_id": "printer_3d_01", "mode": "real"})
        print("ℹ️ Переключение принтера в real:",
              "выполнено" if response and response.get("success") else f"нет ({(response or {}).get('error')})")
    else:
        check("Переключение режима узла",
              await client.request({"type": "switch_mode", "node_id": "printer_3d_01", "mode": "real"}),
              expect_success=False)

    await asyncio.sleep(TELEMETRY_WAIT / 2)
    print("\n" + "#" * 70)
    print(f"# TEST SCENARIO COMPLETE: {sum(results)}/{len(results)} проверок пройдено")
    print("#" * 70)


async def run_client(uri: str, key: str, verbose: bool) -> int:
    logger.info("Подключение к %s...", uri)
    try:
        async with websockets.connect(uri, ping_interval=None, close_timeout=3, max_size=10_000_000) as ws:
            client = DomeTestClient(ws, verbose)
            if not await client.authenticate(key):
                return 1
            receiver = asyncio.create_task(client.receive_loop())
            scenario = asyncio.create_task(run_scenario(client))
            done, pending = await asyncio.wait([receiver, scenario], return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task.exception():
                    raise task.exception()
            return 0 if all(results) else 2
    except ConnectionClosed as e:
        logger.error("Соединение закрыто: code=%s reason=%s", e.code, e.reason)
    except OSError as e:
        logger.error("Ошибка сети: %s", e)
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Тестовый клиент сервера купола")
    parser.add_argument("--host", default=os.getenv("DOME_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DOME_PORT", "8765")))
    parser.add_argument("--key", default=os.getenv("DOME_KEY"), help="ключ доступа (или переменная DOME_KEY)")
    parser.add_argument("--verbose", action="store_true", help="печатать каждую телеметрию целиком")
    args = parser.parse_args()
    if not args.key:
        parser.error("нужен ключ доступа: --key или переменная окружения DOME_KEY")
    try:
        sys.exit(asyncio.run(run_client(f"ws://{args.host}:{args.port}", args.key, args.verbose)))
    except KeyboardInterrupt:
        print("\nКлиент остановлен.")

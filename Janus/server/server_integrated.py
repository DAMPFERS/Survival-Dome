#!/usr/bin/env python3
"""
WebSocket-сервер купола: симулятор + реальное оборудование + авторизация.

Протокол (JSON-сообщения):
    1. Первое сообщение клиента — {"type": "auth", "key": "<ключ>"}.
       Ответ: {"type": "auth_response", "success": true, "role": "admin"|"team", "team": N}.
       Без авторизации за AUTH_TIMEOUT секунд или с неверным ключом соединение закрывается (4401).
    2. Далее сервер каждые TELEMETRY_INTERVAL секунд шлёт {"type": "telemetry", ...}.
       Состав зависит от роли: админ получает всё, участник — только разрешённое
       политикой доступа (authorization/access_policy.json).
    3. Запросы клиента: {"type": "<тип>", "request_id": "...", ...}.
       Ответ: {"type": "<тип>_response", "request_id": "...", "success": ..., ...}.

Типы запросов:
    все роли:  control, describe_nodes, ping
    только админ: switch_mode, trigger_crisis, stop_crisis, time_control,
                  set_active_team, get_policy, set_participant_access, reload_policy

Реальные устройства (real_devices.py) синхронизируются в фоне. Узел в режиме
"real" берёт данные с железа, команды уходят на железо. Если DOME_AUTO_REAL=1,
узел автоматически переходит в "real", как только устройство стало доступно
(пока админ явно не переключил его в "virtual").
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

from websockets import serve
from websockets.exceptions import ConnectionClosed

SERVER_DIR = Path(__file__).parent
sys.path.insert(0, str(SERVER_DIR / "simulation"))
sys.path.insert(0, str(SERVER_DIR))

from authorization.access_policy import ROLE_ADMIN, ROLE_TEAM, AccessPolicy, PolicyError  # noqa: E402
from authorization.protector import Protector  # noqa: E402
from dome_simulator import ControlError, create_dome_simulator  # noqa: E402
from real_devices import RealCommandError, build_adapters  # noqa: E402

# ================= НАСТРОЙКА ЛОГИРОВАНИЯ =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("IntegratedServer")

# ================= КОНФИГУРАЦИЯ =================
HOST = os.getenv("DOME_HOST", "0.0.0.0")
PORT = int(os.getenv("DOME_PORT", "8765"))
TELEMETRY_INTERVAL = float(os.getenv("DOME_TELEMETRY_INTERVAL", "2.0"))
REAL_SYNC_INTERVAL = float(os.getenv("DOME_REAL_SYNC_INTERVAL", "1.0"))
AUTH_TIMEOUT = float(os.getenv("DOME_AUTH_TIMEOUT", "10.0"))
REAL_COMMAND_TIMEOUT = float(os.getenv("DOME_REAL_COMMAND_TIMEOUT", "10.0"))
ACTIVE_TEAM = int(os.getenv("DOME_ACTIVE_TEAM", "1"))           # 1..4
AUTO_REAL = os.getenv("DOME_AUTO_REAL", "1") not in ("0", "false", "False", "")
KEYS_CONFIG = os.getenv("DOME_KEYS_CONFIG", str(SERVER_DIR / "authorization" / "keys_config.json"))
POLICY_FILE = os.getenv("DOME_POLICY_FILE", str(SERVER_DIR / "authorization" / "access_policy.json"))

# Действия, которые есть только у реального устройства (в симуляторе их нет)
REAL_ONLY_ACTIONS = {"cnc_01": ("probe_z",)}

CLOSE_UNAUTHORIZED = 4401
CLOSE_REVOKED = 4403

Mode = Literal["real", "virtual"]


class RequestError(Exception):
    """Ошибка запроса клиента — уходит в ответ как error, без трассировки в логе."""


@dataclass(eq=False)
class Session:
    websocket: Any
    role: str
    team: Optional[int]
    id: int
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send(json.dumps(message, ensure_ascii=False, default=str))

    def __str__(self) -> str:
        who = "admin" if self.role == ROLE_ADMIN else f"team{self.team}"
        return f"#{self.id}({who})"


Handler = Callable[[Session, dict[str, Any]], Awaitable[dict[str, Any]]]


class DomeServer:
    def __init__(self) -> None:
        self.protector = Protector(KEYS_CONFIG)
        self.protector.setActiveTeamKey(ACTIVE_TEAM - 1)
        self.policy = AccessPolicy(POLICY_FILE)

        self.simulator = create_dome_simulator(tick_interval=4.0, time_scale=1.0)
        self.adapters = build_adapters(lambda: self.simulator.get("smart_panel_01", "lines"))
        self.node_modes: dict[str, Mode] = {node_id: "virtual" for node_id in self.adapters}
        self.manual_virtual: set[str] = set()  # узлы, которые админ явно держит виртуальными

        self.sessions: set[Session] = set()
        self._session_ids = itertools.count(1)
        self._sync_task: Optional[asyncio.Task] = None

        self.handlers: dict[str, tuple[Handler, set[str]]] = {
            "ping": (self.handle_ping, {ROLE_ADMIN, ROLE_TEAM}),
            "control": (self.handle_control, {ROLE_ADMIN, ROLE_TEAM}),
            "describe_nodes": (self.handle_describe_nodes, {ROLE_ADMIN, ROLE_TEAM}),
            "switch_mode": (self.handle_switch_mode, {ROLE_ADMIN}),
            "trigger_crisis": (self.handle_trigger_crisis, {ROLE_ADMIN}),
            "stop_crisis": (self.handle_stop_crisis, {ROLE_ADMIN}),
            "time_control": (self.handle_time_control, {ROLE_ADMIN}),
            "set_active_team": (self.handle_set_active_team, {ROLE_ADMIN}),
            "get_policy": (self.handle_get_policy, {ROLE_ADMIN}),
            "set_participant_access": (self.handle_set_participant_access, {ROLE_ADMIN}),
            "reload_policy": (self.handle_reload_policy, {ROLE_ADMIN}),
        }

    # ================= ЖИЗНЕННЫЙ ЦИКЛ =================

    async def start(self) -> None:
        self.simulator.start()
        logger.info("Dome simulator started")
        for adapter in self.adapters.values():
            try:
                await asyncio.to_thread(adapter.start)
            except Exception as e:
                logger.error("Не удалось запустить %s: %s", adapter.title, e)
        logger.info("Real device adapters: %s", ", ".join(
            f"{nid} ({a.title})" for nid, a in self.adapters.items()) or "нет")
        self._sync_task = asyncio.create_task(self._real_sync_loop())

    async def stop(self) -> None:
        if self._sync_task:
            self._sync_task.cancel()
        self.simulator.stop()
        for adapter in self.adapters.values():
            try:
                await asyncio.to_thread(adapter.stop)
            except Exception as e:
                logger.error("Ошибка остановки %s: %s", adapter.title, e)
        logger.info("Server stopped")

    # ================= РЕАЛЬНОЕ ОБОРУДОВАНИЕ =================

    async def _real_sync_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.sync_real_to_simulator)
            except Exception:
                logger.exception("Ошибка синхронизации реальных устройств")
            await asyncio.sleep(REAL_SYNC_INTERVAL)

    def sync_real_to_simulator(self) -> None:
        """Переносит данные реальных устройств в узлы, работающие в режиме real.
        Выполняется в отдельном потоке (опрос драйверов может блокировать)."""
        for node_id, adapter in self.adapters.items():
            if (self.node_modes[node_id] == "virtual" and AUTO_REAL
                    and node_id not in self.manual_virtual and adapter.available()):
                self.node_modes[node_id] = "real"
                logger.info("%s: устройство доступно — узел переведён в REAL", node_id)
            if self.node_modes[node_id] == "real":
                self.sync_node(node_id)

    def sync_node(self, node_id: str) -> None:
        try:
            updates = self.adapters[node_id].read()
            if updates:
                updates["control_override_source"] = "real"
                self.simulator.update(node_id, **updates)
        except Exception as e:
            logger.error("Синхронизация %s не удалась: %s", node_id, e)

    # ================= ТЕЛЕМЕТРИЯ =================

    def build_telemetry(self, session: Session) -> dict[str, Any]:
        snap = self.simulator.snapshot()
        env = self.simulator.environment_snapshot()
        tc = self.simulator.time_controller
        full = {
            "nodes": snap["nodes"],
            "game_time": {"day": env["outdoor"]["day"], "hour": round(env["outdoor"]["hour"], 2)},
            "node_modes": dict(self.node_modes),
            "active_crises": self.simulator.active_crises(),
            "sim_time": tc.sim_time,
            "time_scale": tc.time_scale,
            "paused": tc.is_paused,
            "environment": env,
            "active_team": (self.protector.getActiveTeamIndex() or 0) + 1,
            "real_devices": {nid: a.status() for nid, a in self.adapters.items()},
        }
        if session.role == ROLE_ADMIN:
            data = full
        else:
            fields = self.policy.telemetry_fields(session.role) or set()
            data = {"nodes": self.policy.filter_nodes(session.role, snap["nodes"])}
            data.update({k: full[k] for k in fields if k in full and k != "nodes"})
        return {"type": "telemetry", "timestamp": snap["timestamp"], "role": session.role, "data": data}

    async def _telemetry_loop(self, session: Session) -> None:
        try:
            while True:
                await session.send(self.build_telemetry(session))
                await asyncio.sleep(TELEMETRY_INTERVAL)
        except ConnectionClosed:
            pass

    # ================= ОБРАБОТЧИКИ ЗАПРОСОВ =================

    async def handle_ping(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True}

    def _require_node(self, session: Session, node_id: Any) -> str:
        if not isinstance(node_id, str) or node_id not in self.simulator.node_ids() \
                or not self.policy.node_visible(session.role, node_id):
            raise RequestError(f"Неизвестный узел: {node_id!r}")  # скрытый узел не выдаём
        return node_id

    async def handle_control(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        node_id = self._require_node(session, data.get("node_id"))
        action = data.get("action")
        if not isinstance(action, str) or not action.strip():
            raise RequestError("Не указано действие (action)")
        value = data.get("value")
        if data.get("job_name") is not None and value is None:  # совместимость со старым API
            value = {"file_name": data["job_name"]}
            if data.get("duration_s") is not None:
                value["duration_s"] = data["duration_s"]

        canonical = self.simulator.resolve_action(node_id, action) or action.strip().lower()
        if not self.policy.can_control(session.role, node_id, canonical):
            allowed = self._allowed_controls(session, node_id)
            raise RequestError(f"Действие {action!r} для {node_id} недоступно. "
                               f"Разрешено: {allowed or 'ничего'}")

        mode = self.node_modes.get(node_id, "virtual")
        if mode == "real":
            try:
                await asyncio.wait_for(self.adapters[node_id].execute(canonical, value), REAL_COMMAND_TIMEOUT)
            except RealCommandError as e:
                raise RequestError(str(e)) from None
            except asyncio.TimeoutError:
                raise RequestError(f"Реальное устройство не ответило за {REAL_COMMAND_TIMEOUT:.0f} с") from None
            await asyncio.to_thread(self.sync_node, node_id)  # сразу показываем результат
        else:
            try:
                self.simulator.control(node_id, action, value)
            except ControlError as e:
                if session.role != ROLE_ADMIN and "Доступно:" in str(e):
                    raise RequestError(f"Узел {node_id} не поддерживает действие {action!r}. "
                                       f"Разрешено: {self._allowed_controls(session, node_id)}") from None
                raise RequestError(str(e)) from None

        logger.info("%s control %s.%s(%r) [%s]", session, node_id, canonical, value, mode)
        return {"success": True, "node_id": node_id, "action": action, "mode": mode}

    def _node_controls(self, node_id: str) -> list[str]:
        return next(n["controls"] for n in self.simulator.describe_nodes() if n["node_id"] == node_id)

    def _allowed_controls(self, session: Session, node_id: str) -> list[str]:
        return self.policy.allowed_actions(session.role, node_id, self._node_controls(node_id))

    async def handle_describe_nodes(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        nodes = []
        for info in self.simulator.describe_nodes():
            node_id = info["node_id"]
            if not self.policy.node_visible(session.role, node_id):
                continue
            if session.role == ROLE_ADMIN:
                adapter = self.adapters.get(node_id)
                info["mode"] = self.node_modes.get(node_id, "virtual")
                info["real_device"] = adapter.status() if adapter else None
                info["real_only_controls"] = list(REAL_ONLY_ACTIONS.get(node_id, ()))
            else:
                allowed = self.policy.allowed_actions(session.role, node_id, info["controls"])
                info["controls"] = allowed
                info["control_aliases"] = {k: v for k, v in info["control_aliases"].items() if k in allowed}
            nodes.append(info)
        return {"success": True, "nodes": nodes}

    async def handle_switch_mode(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        node_id, new_mode = data.get("node_id"), data.get("mode")
        if new_mode not in ("real", "virtual"):
            raise RequestError('mode должен быть "real" или "virtual"')
        adapter = self.adapters.get(node_id)
        if adapter is None:
            raise RequestError(f"Для узла {node_id!r} нет реального устройства")

        if new_mode == "virtual":
            self.node_modes[node_id] = "virtual"
            self.manual_virtual.add(node_id)
            self.simulator.set(node_id, "control_override_source", None)
        else:
            if not await asyncio.to_thread(adapter.available):
                raise RequestError(f"Реальное устройство {adapter.title} недоступно")
            self.manual_virtual.discard(node_id)
            self.node_modes[node_id] = "real"
            await asyncio.to_thread(self.sync_node, node_id)
        logger.info("%s switched %s to %s", session, node_id, new_mode.upper())
        return {"success": True, "node_id": node_id, "mode": new_mode}

    async def handle_trigger_crisis(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        name = data.get("crisis_name")
        if not name:
            raise RequestError("Не указан crisis_name")
        try:
            self.simulator.trigger_crisis(name, data.get("params"))
        except KeyError as e:
            raise RequestError(str(e.args[0] if e.args else e)) from None
        return {"success": True, "crisis_name": name}

    async def handle_stop_crisis(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        name = data.get("crisis_name")
        if not name:
            raise RequestError("Не указан crisis_name")
        self.simulator.stop_crisis(name)
        return {"success": True, "crisis_name": name}

    async def handle_time_control(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        action = data.get("action")
        if action == "set_scale":
            try:
                self.simulator.set_time_scale(float(data.get("value")))
            except (TypeError, ValueError) as e:
                raise RequestError(f"Некорректный time_scale: {e}") from None
        elif action == "pause":
            self.simulator.pause()
        elif action == "resume":
            self.simulator.resume()
        else:
            raise RequestError(f"Неизвестное действие времени: {action!r} (set_scale, pause, resume)")
        return {"success": True, "action": action}

    async def handle_set_active_team(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        try:
            team = int(data.get("team"))
            self.protector.setActiveTeamKey(team - 1)
        except (TypeError, ValueError):
            raise RequestError("team: ожидался номер команды 1..4") from None
        revoked = [s for s in self.sessions if s.role == ROLE_TEAM and s.team != team]
        for s in revoked:
            asyncio.create_task(self._revoke(s, f"Активная команда сменилась на {team}"))
        logger.info("%s set active team %d (отключено сессий: %d)", session, team, len(revoked))
        return {"success": True, "team": team, "revoked_sessions": len(revoked)}

    async def _revoke(self, session: Session, reason: str) -> None:
        try:
            await session.send({"type": "session_revoked", "reason": reason})
            await session.websocket.close(CLOSE_REVOKED, "session revoked")
        except ConnectionClosed:
            pass

    async def handle_get_policy(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "policy": self.policy.snapshot()}

    async def handle_set_participant_access(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        """{"node_id": "...", "actions": [...] | "*", "visible": bool, "hidden_params": [...]}"""
        node_id = data.get("node_id")
        if node_id not in self.simulator.node_ids():
            raise RequestError(f"Неизвестный узел: {node_id!r}")
        changes = {k: data[k] for k in ("actions", "visible", "hidden_params") if k in data}
        if not changes:
            raise RequestError("Нужно указать хотя бы одно из: actions, visible, hidden_params")
        if isinstance(changes.get("actions"), list):
            valid = set(self._node_controls(node_id)) | set(REAL_ONLY_ACTIONS.get(node_id, ()))
            unknown = [a for a in changes["actions"] if not isinstance(a, str) or a.lower() not in valid]
            if unknown:
                raise RequestError(f"Неизвестные действия {unknown}. Доступно: {sorted(valid)}")
        try:
            rule = self.policy.update_node(node_id, changes)
        except (PolicyError, OSError) as e:
            raise RequestError(f"Политика не изменена: {e}") from None
        logger.info("%s changed participant access for %s: %s", session, node_id, changes)
        return {"success": True, "node_id": node_id, "rule": rule}

    async def handle_reload_policy(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        if not self.policy.reload():
            raise RequestError("Файл политики некорректен — оставлена прежняя версия (см. лог сервера)")
        return {"success": True}

    # ================= СОЕДИНЕНИЯ =================

    async def _authenticate(self, websocket) -> Optional[Session]:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT)
            data = json.loads(raw)
        except asyncio.TimeoutError:
            await websocket.close(CLOSE_UNAUTHORIZED, "auth timeout")
            return None
        except (json.JSONDecodeError, TypeError):
            data = {}

        request_id = data.get("request_id") if isinstance(data, dict) else None
        role = None
        if isinstance(data, dict) and data.get("type") == "auth":
            role = self.protector.getRole(data.get("key"))
        if role is None:
            logger.warning("Отказ в авторизации: %s", websocket.remote_address)
            await asyncio.sleep(1.0)  # замедляем перебор ключей
            await websocket.send(json.dumps({"type": "auth_response", "request_id": request_id,
                                             "success": False, "error": "Неверный ключ доступа"}))
            await websocket.close(CLOSE_UNAUTHORIZED, "unauthorized")
            return None

        team = (self.protector.getActiveTeamIndex() or 0) + 1 if role == ROLE_TEAM else None
        session = Session(websocket=websocket, role=role, team=team, id=next(self._session_ids))
        await session.send({"type": "auth_response", "request_id": request_id, "success": True,
                            "role": role, "team": team})
        logger.info("Client %s authorized as %s", websocket.remote_address, session)
        return session

    async def _dispatch(self, session: Session, raw: str) -> None:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            await session.send({"type": "error", "success": False, "error": "Некорректный JSON"})
            return

        msg_type = data.get("type")
        response: dict[str, Any]
        entry = self.handlers.get(msg_type)
        if entry is None:
            response = {"success": False, "error": f"Неизвестный тип сообщения: {msg_type!r}"}
        elif session.role not in entry[1]:
            response = {"success": False, "error": "Недостаточно прав"}
        else:
            try:
                response = await entry[0](session, data)
            except RequestError as e:
                response = {"success": False, "error": str(e)}
            except Exception as e:
                logger.exception("Ошибка обработки %s от %s", msg_type, session)
                response = {"success": False, "error": f"Внутренняя ошибка сервера: {e}"}

        if not response.get("success") and msg_type == "control":
            response.setdefault("node_id", data.get("node_id"))
            response.setdefault("action", data.get("action"))
        response["type"] = f"{msg_type}_response"
        if "request_id" in data:
            response["request_id"] = data["request_id"]
        await session.send(response)

    async def handle_client(self, websocket) -> None:
        logger.info("Client connected: %s", websocket.remote_address)
        session = await self._authenticate(websocket)
        if session is None:
            return
        self.sessions.add(session)
        telemetry = asyncio.create_task(self._telemetry_loop(session))
        try:
            async for raw in websocket:
                await self._dispatch(session, raw)
        except ConnectionClosed:
            pass
        finally:
            telemetry.cancel()
            self.sessions.discard(session)
            logger.info("Client disconnected: %s", session)


# ================= ЗАПУСК =================

async def main() -> None:
    try:
        server = DomeServer()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Авторизация не настроена: %s", e)
        logger.error("Создайте ключи: python authorization/generate_keys.py")
        return

    await server.start()
    try:
        async with serve(server.handle_client, HOST, PORT, max_size=2_000_000):
            logger.info("WebSocket server started on ws://%s:%d (active team: %d)", HOST, PORT, ACTIVE_TEAM)
            await asyncio.Future()
    except asyncio.CancelledError:
        logger.info("Server task cancelled.")
    finally:
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

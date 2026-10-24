#!/usr/bin/env python3
"""
WebSocket-сервер купола: симулятор + реальное оборудование + авторизация.

Протокол (JSON-сообщения):
    1. Первое сообщение клиента — {"type": "auth", "key": "<ключ>"}.
       Ответ: {"type": "auth_response", "success": true, "role": "admin"|"team"|"guest", "team": N}.
       "team" — ключ активной команды; "guest" — ключ неактивной команды: доступна
       только песочница (meeting_01). Без авторизации за AUTH_TIMEOUT секунд или
       с неверным ключом соединение закрывается (4401).
    2. Далее сервер каждые TELEMETRY_INTERVAL секунд шлёт {"type": "telemetry", ...}.
       Состав зависит от роли: админ получает всё, участник — только разрешённое
       политикой доступа (authorization/access_policy.json).
    3. Запросы клиента: {"type": "<тип>", "request_id": "...", ...}.
       Ответ: {"type": "<тип>_response", "request_id": "...", "success": ..., ...}.

Типы запросов:
    все роли:  control, query, describe_nodes, ping
    только админ: switch_mode, trigger_crisis, stop_crisis, list_crises, start_shift,
                  shift_report, time_control, set_active_team, get_policy,
                  set_participant_access, reload_policy

query — чтение данных узла, которых нет в телеметрии:
    {"type": "query", "node_id": "printer_3d_01", "query": "list_files", "params": {...}}
    -> {"type": "query_response", "success": true, "result": ...}
    Список запросов узла — поле "queries" в describe_nodes.

Песочница meeting_01 — общий чат агентов всех команд и Оператора (meeting/meeting_room.py):
    control post_message, value = "текст" | {"text", "name"?: имя агента, "to"?: адресат};
    query read_messages, params = {"since_id"?, "limit"?}; админ — control clear.
    Новое сообщение сразу рассылается всем, кто видит песочницу:
    {"type": "meeting_message", "message": {...}}; в телеметрии узла — последние сообщения.
    Отправителя (роль, команда) проставляет сервер. Кризисы связи на чат не влияют.
    Сообщения активной команды и Оператора видят кризисы (simulator.record_chat):
    часть кризисов требует уведомить Оператора. Реплики виртуального Оператора
    сценария смены (события operator.say: задания, просьбы) сервер публикует в песочнице
    от имени {"role": "operator", "name": "Оператор"}.

Станки (dome_crises.md, п. 3.8): паузу и отмену задания ставит только сервер
(интерлок или кризис) — поля hold_level / hold_reason / resume_attempts /
lockout_remaining_s в телеметрии принтера и фрезера. resume агента проходит, только
если причина устранена; третья пустая попытка — ALARM на 20 минут (принтер: задание
отменено). Линии щитка могут быть заморожены (lines[i].lockout_remaining_s).

Телеметрия участника — то, что видит ИИ-агент (dome_crises.md): подмены
показаний, заморозки узлов, обрывы связи. Админ дополнительно получает
истинное состояние (true_nodes), отчёты кризисов и ход смены.

Команды участника идут в симулятор с origin="agent": при обрыве связи они не
проходят, физические действия Оператора (замена патчкорда и т.п.) недоступны.
Админ выполняет любые действия, включая действия Оператора (origin="operator").

Реальные устройства (real_devices.py) синхронизируются в фоне. Узел в режиме
"real" берёт данные с железа, команды уходят на железо. Если DOME_AUTO_REAL=1,
узел автоматически переходит в "real", как только устройство стало доступно
(пока админ явно не переключил его в "virtual").

Команды реальному узлу сначала проходят через симулятор (prepare_real_command):
кризисы могут перехватить команду-решение и подменить её безопасным реальным
эквивалентом (Д7). Команды автоматики и кризисов реальным устройствам
(пауза станков, реле щита при блэкауте виртуальной энергосистемы — Д19)
забираются из simulator.drain_real_commands() в цикле синхронизации.

Журнал участника (logger/participant_log.py, DOME_PARTICIPANT_LOG, по умолчанию logs/participant):
то, что видит команда, — телеметрия после фильтра политики (снимок раз в
DOME_PARTICIPANT_LOG_SNAPSHOT_S, изменения состояний — сразу), команды ключа команды с ответами,
физические действия Оператора из админки и сообщения песочницы. Действия организаторов
(кризисы, время, политика) и истинное состояние в журнал не пишутся.

Энергосистема (DOME_ENERGY_MODE):
    virtual (по умолчанию) — инвертор и АКБ на время смены виртуальные с одинаковым
                              стартом; показания реального инвертора пишутся для админа;
    demo                   — виртуальная энергосистема, но генерация PV — реальная;
    real                   — инвертор и АКБ берут данные с железа (как раньше).
DOME_BLACKOUT_RELAYS=1 (по умолчанию) — блэкаут виртуальной энергосистемы (E02/E04/E07)
    физически выключает реле щитка (Д19, нужно для К1, №31, К3); 0 — реле не трогаются (отладка).
DOME_INTERLOCKS=1 (по умолчанию) — интерлоки автоматики (IL_LOAD_SHED, IL_OVERCURRENT, IL_SMOKE…);
    0 — не срабатывают и ничего не отключают (отладка; для смены не годится).

Время купола (задаётся при запуске, на ходу не меняется):
    DOME_TIME_MODE=hybrid (по умолчанию) — игровые сутки 3 ч: несколько прогонов смены за день;
    DOME_TIME_MODE=real                  — сутки 24 ч, часы купола идут как настоящие.
    DOME_START_HOUR — час на часах купола при запуске: число (13.5 = 13:30) или now
    (по умолчанию hybrid — 8, real — now). Физика устройств и таймеры кризисов в обоих
    режимах идут 1:1. В режиме real start_shift без start_hour начинает смену с текущего часа.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import queue
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

from websockets import serve
from websockets.exceptions import ConnectionClosed

SERVER_DIR = Path(__file__).parent
sys.path.insert(0, str(SERVER_DIR / "simulation"))
sys.path.insert(0, str(SERVER_DIR))

from authorization.access_policy import ROLE_ADMIN, ROLE_GUEST, ROLE_TEAM, AccessPolicy, PolicyError  # noqa: E402
from authorization.protector import Protector  # noqa: E402
from dome_simulator import ControlError, create_dome_simulator  # noqa: E402
from logger.participant_log import ParticipantLogger  # noqa: E402
from meeting.meeting_room import NODE_ID as MEETING_NODE, MeetingError, MeetingRoom  # noqa: E402
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
REAL_UPLOAD_TIMEOUT = float(os.getenv("DOME_REAL_UPLOAD_TIMEOUT", "60.0"))   # загрузка файла на принтер
MAX_MESSAGE = int(os.getenv("DOME_MAX_MESSAGE", str(32 * 1024 * 1024)))     # байт; G-code в upload_file
MEETING_FILE = os.getenv("DOME_MEETING_FILE", str(SERVER_DIR / "meeting" / "history.jsonl"))
MEETING_RATE = (10, 10.0)   # не больше 10 сообщений песочницы за 10 с с одной сессии
ACTIVE_TEAM = int(os.getenv("DOME_ACTIVE_TEAM", "1"))           # 1..4
AUTO_REAL = os.getenv("DOME_AUTO_REAL", "1") not in ("0", "false", "False", "")
ENERGY_MODE = os.getenv("DOME_ENERGY_MODE", "virtual").strip().lower()   # virtual | demo | real
# Блэкаут виртуальной энергосистемы выключает реальные реле щитка (Д19). 0 — для отладки железа.
BLACKOUT_RELAYS = os.getenv("DOME_BLACKOUT_RELAYS", "1") not in ("0", "false", "False", "")
# Интерлоки автоматики (сброс нагрузки, токовая защита, дым…). 0 — для отладки железа.
INTERLOCKS = os.getenv("DOME_INTERLOCKS", "1") not in ("0", "false", "False", "")
# Время купола: hybrid — игровые сутки 3 ч (несколько прогонов смены за день),
# real — сутки 24 ч, часы купола идут как настоящие. Физика устройств в обоих режимах 1:1.
TIME_MODE = os.getenv("DOME_TIME_MODE", "hybrid").strip().lower()       # hybrid | real
DAY_LENGTH_S = {"hybrid": 3 * 3600.0, "real": 24 * 3600.0}
# Час на часах купола при запуске: число (13.5 = 13:30) или "now" — текущее местное время.
# По умолчанию: hybrid — 8:00, real — now.
START_HOUR = os.getenv("DOME_START_HOUR", "").strip().lower()
ENERGY_NODES = ("solar_inverter_01", "battery_01")
# Журнал участника (logger/participant_log.py): телеметрия команды, её команды, действия Оператора
# и сообщения песочницы — JSONL по дням. Пустое значение, off или none — журнал не пишется.
PARTICIPANT_LOG = os.getenv("DOME_PARTICIPANT_LOG", str(SERVER_DIR / "logs" / "participant")).strip()
PARTICIPANT_LOG_SNAPSHOT_S = float(os.getenv("DOME_PARTICIPANT_LOG_SNAPSHOT_S", "60"))
KEYS_CONFIG = os.getenv("DOME_KEYS_CONFIG", str(SERVER_DIR / "authorization" / "keys_config.json"))
POLICY_FILE = os.getenv("DOME_POLICY_FILE", str(SERVER_DIR / "authorization" / "access_policy.json"))

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
    posts: deque = field(default_factory=deque)   # время последних сообщений в песочницу

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send(json.dumps(message, ensure_ascii=False, default=str))

    def __str__(self) -> str:
        who = "admin" if self.role == ROLE_ADMIN else f"{self.role}{self.team}"
        return f"#{self.id}({who})"


Handler = Callable[[Session, dict[str, Any]], Awaitable[dict[str, Any]]]


class DomeServer:
    def __init__(self) -> None:
        self.protector = Protector(KEYS_CONFIG)
        self.protector.setActiveTeamKey(ACTIVE_TEAM - 1)
        self.policy = AccessPolicy(POLICY_FILE)

        self.meeting = MeetingRoom(MEETING_FILE)
        self.simulator = create_dome_simulator(tick_interval=4.0, time_scale=1.0,
                                               day_length_s=DAY_LENGTH_S[TIME_MODE], start_hour=_start_hour())
        if ENERGY_MODE in ("virtual", "demo"):
            self.simulator.set_energy_mode(ENERGY_MODE)
        self.simulator.set_blackout_relays(BLACKOUT_RELAYS)
        if not BLACKOUT_RELAYS:
            logger.warning("DOME_BLACKOUT_RELAYS=0: блэкаут не выключает реальные реле "
                           "(К1, №31, К3 на щитке не сработают)")
        self.simulator.set_interlocks(INTERLOCKS)
        if not INTERLOCKS:
            logger.warning("DOME_INTERLOCKS=0: интерлоки автоматики отключены (сценарий смены не оценивается)")
        self.adapters = build_adapters(lambda: self.simulator.get("smart_panel_01", "lines"))
        self.node_modes: dict[str, Mode] = {node_id: "virtual" for node_id in self.adapters}
        self.manual_virtual: set[str] = set()  # узлы, которые админ явно держит виртуальными

        self.plog: Optional[ParticipantLogger] = None
        if PARTICIPANT_LOG.lower() not in ("", "off", "none", "0"):
            self.plog = ParticipantLogger(PARTICIPANT_LOG, snapshot_s=PARTICIPANT_LOG_SNAPSHOT_S)

        self.sessions: set[Session] = set()
        self._session_ids = itertools.count(1)
        self._sync_task: Optional[asyncio.Task] = None
        self._operator_task: Optional[asyncio.Task] = None
        self._plog_task: Optional[asyncio.Task] = None
        # реплики виртуального Оператора (поток симулятора -> цикл событий сервера)
        self._operator_lines: queue.SimpleQueue = queue.SimpleQueue()
        self.simulator.subscribe("operator.say", lambda event: self._operator_lines.put(dict(event.payload)))

        everyone = {ROLE_ADMIN, ROLE_TEAM, ROLE_GUEST}
        self.handlers: dict[str, tuple[Handler, set[str]]] = {
            "ping": (self.handle_ping, everyone),
            "control": (self.handle_control, everyone),
            "query": (self.handle_query, everyone),
            "describe_nodes": (self.handle_describe_nodes, everyone),
            "switch_mode": (self.handle_switch_mode, {ROLE_ADMIN}),
            "trigger_crisis": (self.handle_trigger_crisis, {ROLE_ADMIN}),
            "stop_crisis": (self.handle_stop_crisis, {ROLE_ADMIN}),
            "list_crises": (self.handle_list_crises, {ROLE_ADMIN}),
            "start_shift": (self.handle_start_shift, {ROLE_ADMIN}),
            "shift_report": (self.handle_shift_report, {ROLE_ADMIN}),
            "time_control": (self.handle_time_control, {ROLE_ADMIN}),
            "set_active_team": (self.handle_set_active_team, {ROLE_ADMIN}),
            "get_policy": (self.handle_get_policy, {ROLE_ADMIN}),
            "set_participant_access": (self.handle_set_participant_access, {ROLE_ADMIN}),
            "reload_policy": (self.handle_reload_policy, {ROLE_ADMIN}),
        }

    # ================= ЖИЗНЕННЫЙ ЦИКЛ =================

    async def start(self) -> None:
        self.simulator.start()
        env = self.simulator.environment
        logger.info("Dome simulator started: время %s (сутки %.0f ч), часы купола %02d:%02d", TIME_MODE,
                    env.day_length_s / 3600, int(env.outdoor.hour), int(env.outdoor.hour % 1 * 60))
        for adapter in self.adapters.values():
            try:
                await asyncio.to_thread(adapter.start)
            except Exception as e:
                logger.error("Не удалось запустить %s: %s", adapter.title, e)
        logger.info("Real device adapters: %s", ", ".join(
            f"{nid} ({a.title})" for nid, a in self.adapters.items()) or "нет")
        self._sync_task = asyncio.create_task(self._real_sync_loop())
        self._operator_task = asyncio.create_task(self._operator_loop())
        if self.plog is not None:
            self._plog_task = asyncio.create_task(self._participant_log_loop())
            logger.info("Журнал участника: %s (снимок телеметрии раз в %.0f с)", PARTICIPANT_LOG,
                        PARTICIPANT_LOG_SNAPSHOT_S)

    async def stop(self) -> None:
        if self._sync_task:
            self._sync_task.cancel()
        if self._operator_task:
            self._operator_task.cancel()
        if self._plog_task:
            self._plog_task.cancel()
        if self.plog is not None:
            self.plog.close()
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
        """Переносит данные реальных устройств в узлы, работающие в режиме real,
        и исполняет команды автоматики/кризисов на реальном железе.
        Выполняется в отдельном потоке (опрос драйверов может блокировать)."""
        for node_id, adapter in self.adapters.items():
            if node_id in ENERGY_NODES and ENERGY_MODE != "real":
                # энергосистема смены виртуальная: реальные показания — для админа и PV в демо-режиме
                if node_id == "solar_inverter_01":
                    try:
                        reading = adapter.read()
                        if reading:
                            self.simulator.feed_real_inverter(reading)
                    except Exception as e:
                        logger.error("Чтение реального инвертора не удалось: %s", e)
                continue
            if (self.node_modes[node_id] == "virtual" and AUTO_REAL
                    and node_id not in self.manual_virtual and adapter.available()):
                self.node_modes[node_id] = "real"
                logger.info("%s: устройство доступно — узел переведён в REAL", node_id)
            if self.node_modes[node_id] == "real":
                self.sync_node(node_id)
        self.execute_pending_real_commands()

    def execute_pending_real_commands(self) -> None:
        """Команды реальным устройствам от автоматики и кризисов (пауза станков,
        реле щита при блэкауте виртуальной энергосистемы). Вызывается из потока синхронизации."""
        for node_id, action, value in self.simulator.drain_real_commands():
            adapter = self.adapters.get(node_id)
            if adapter is None or self.node_modes.get(node_id) != "real":
                continue
            try:
                asyncio.run(asyncio.wait_for(adapter.execute(action, value), REAL_COMMAND_TIMEOUT))
                logger.info("auto -> %s.%s(%r) [real]", node_id, action, value)
            except Exception as e:
                logger.error("Команда %s.%s(%r) реальному устройству не выполнена: %s", node_id, action, value, e)

    def sync_node(self, node_id: str) -> None:
        try:
            updates = self.adapters[node_id].read()
            if updates:
                updates["control_override_source"] = "real"
                self.simulator.update(node_id, **updates)
        except Exception as e:
            logger.error("Синхронизация %s не удалась: %s", node_id, e)

    # ================= ЖУРНАЛ УЧАСТНИКА =================

    def _dome_time(self) -> dict[str, Any]:
        outdoor = self.simulator.environment.outdoor
        return {"day": int(outdoor.day) + 1, "time": f"{int(outdoor.hour) % 24:02d}:{int(outdoor.hour % 1 * 60):02d}"}

    async def _participant_log_loop(self) -> None:
        """Телеметрия, которую видит команда (без песочницы — её сообщения пишутся отдельно)."""
        while True:
            try:
                nodes = self.policy.filter_nodes(ROLE_TEAM, self.simulator.snapshot()["nodes"])
                self.plog.observe(datetime.now(), nodes, dome=self._dome_time())
            except Exception:
                logger.exception("Журнал участника: ошибка записи телеметрии")
            await asyncio.sleep(TELEMETRY_INTERVAL)

    def _log_control(self, session: Session, data: dict[str, Any], response: dict[str, Any]) -> None:
        """Команда участника (ключ активной команды) или физическое действие Оператора из админки.
        Прочие действия админа (организаторов) и сообщения песочницы сюда не попадают."""
        if self.plog is None:
            return
        node_id, action = data.get("node_id"), data.get("action")
        if node_id == MEETING_NODE or not isinstance(node_id, str) or not isinstance(action, str):
            return
        if session.role == ROLE_TEAM:
            actor = "agent"
        elif session.role == ROLE_ADMIN and node_id in self.simulator.node_ids():
            canonical = self.simulator.resolve_action(node_id, action) or action.strip().lower()
            if self._origin(session, node_id, canonical) != "operator":
                return
            actor = "operator"
        else:
            return
        self.plog.action(datetime.now(), actor, node_id, action, data.get("value"), bool(response.get("success")),
                         response.get("error"), team=session.team if actor == "agent" else None,
                         dome=self._dome_time())

    def _log_message(self, message: dict[str, Any]) -> None:
        if self.plog is not None:
            self.plog.message(datetime.now(), message, dome=self._dome_time())

    # ================= ТЕЛЕМЕТРИЯ =================

    def build_telemetry(self, session: Session) -> dict[str, Any]:
        snap = self.simulator.snapshot()          # телеметрия, которую видит агент
        nodes = {**snap["nodes"], MEETING_NODE: self.meeting.summary()}
        if session.role == ROLE_GUEST:
            return {"type": "telemetry", "timestamp": snap["timestamp"], "role": session.role,
                    "data": {"nodes": self.policy.filter_nodes(session.role, nodes)}}
        env = self.simulator.environment_snapshot()
        tc = self.simulator.time_controller
        full = {
            "nodes": nodes,
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
            data.update({
                "true_nodes": self.simulator.snapshot(view="truth")["nodes"],
                "crisis_reports": self.simulator.crisis_reports(),
                "telemetry_layer": self.simulator.telemetry.describe(),
                "energy_mode": ENERGY_MODE,
                "blackout_relays": BLACKOUT_RELAYS,
                "interlocks": INTERLOCKS,
                "time_mode": TIME_MODE,
                "real_inverter": dict(self.simulator.ctx.real_inverter_reading),
            })
            if self.simulator.shift is not None:
                shift = self.simulator.shift
                data["shift"] = {"t_s": round(shift.t, 1), "finished": shift.finished,
                                 "metrics": shift.metrics.to_dict()}
        else:
            fields = self.policy.telemetry_fields(session.role) or set()
            data = {"nodes": self.policy.filter_nodes(session.role, nodes)}   # с песочницей meeting_01
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
        if not isinstance(node_id, str) or node_id not in (*self.simulator.node_ids(), MEETING_NODE) \
                or not self.policy.node_visible(session.role, node_id):
            raise RequestError(f"Неизвестный узел: {node_id!r}")  # скрытый узел не выдаём
        return node_id

    async def handle_control(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        node_id = self._require_node(session, data.get("node_id"))
        action = data.get("action")
        if not isinstance(action, str) or not action.strip():
            raise RequestError("Не указано действие (action)")
        value = data.get("value")
        if node_id == MEETING_NODE:
            return await self._meeting_control(session, action, value)
        if data.get("job_name") is not None and value is None:  # совместимость со старым API
            value = {"file_name": data["job_name"]}
            if data.get("duration_s") is not None:
                value["duration_s"] = data["duration_s"]

        canonical = self.simulator.resolve_action(node_id, action) or action.strip().lower()
        if canonical != "reconnect" and not self.policy.can_control(session.role, node_id, canonical):
            allowed = self._allowed_controls(session, node_id)
            raise RequestError(f"Действие {action!r} для {node_id} недоступно. "
                               f"Разрешено: {allowed or 'ничего'}")

        origin = self._origin(session, node_id, canonical)
        mode = self.node_modes.get(node_id, "virtual")
        if mode == "real":
            try:
                # кризис может перехватить команду и заменить её безопасным реальным эквивалентом (Д7)
                real_actions = self.simulator.prepare_real_command(node_id, action, value, origin=origin)
            except ControlError as e:
                raise RequestError(str(e)) from None
            for real_action, real_value in real_actions:
                timeout = REAL_UPLOAD_TIMEOUT if real_action == "upload_file" else REAL_COMMAND_TIMEOUT
                try:
                    await asyncio.wait_for(self.adapters[node_id].execute(real_action, real_value), timeout)
                except RealCommandError as e:
                    raise RequestError(str(e)) from None
                except asyncio.TimeoutError:
                    raise RequestError(f"Реальное устройство не ответило за {timeout:.0f} с") from None
            await asyncio.to_thread(self.sync_node, node_id)  # сразу показываем результат
        else:
            try:
                self.simulator.control(node_id, action, value, origin=origin)
            except ControlError as e:
                if session.role != ROLE_ADMIN and "Доступно:" in str(e):
                    raise RequestError(f"Узел {node_id} не поддерживает действие {action!r}. "
                                       f"Разрешено: {self._allowed_controls(session, node_id)}") from None
                raise RequestError(str(e)) from None

        logger.info("%s control %s.%s(%s) [%s]", session, node_id, canonical, _short(value), mode)
        return {"success": True, "node_id": node_id, "action": action, "mode": mode}

    async def handle_query(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        """{"node_id": ..., "query": "list_files", "params": {...}} — данные узла (только чтение)."""
        node_id = self._require_node(session, data.get("node_id"))
        name = data.get("query")
        if not isinstance(name, str) or not name.strip():
            raise RequestError("Не указан запрос (query)")
        name, params = name.strip().lower(), data.get("params")
        response = {"success": True, "node_id": node_id, "query": name}
        if node_id == MEETING_NODE:
            if name != "read_messages":
                raise RequestError(f"Песочница не поддерживает запрос {name!r}. Доступно: ['read_messages']")
            try:
                return {**response, "result": self.meeting.read(params)}
            except MeetingError as e:
                raise RequestError(str(e)) from None

        queries = next(n["queries"] for n in self.simulator.describe_nodes() if n["node_id"] == node_id)
        if name not in queries:
            raise RequestError(f"Узел {node_id} не поддерживает запрос {name!r}. Доступно: {queries}")
        origin = "admin" if session.role == ROLE_ADMIN else "agent"
        mode = self.node_modes.get(node_id, "virtual")
        if mode == "real":
            reason = self.simulator.telemetry.agent_command_block(node_id) if origin == "agent" else None
            if reason:
                raise RequestError(reason)
            try:
                result = await asyncio.wait_for(self.adapters[node_id].query(name, params), REAL_COMMAND_TIMEOUT)
            except RealCommandError as e:
                raise RequestError(str(e)) from None
            except asyncio.TimeoutError:
                raise RequestError(f"Реальное устройство не ответило за {REAL_COMMAND_TIMEOUT:.0f} с") from None
        else:
            try:
                result = self.simulator.query(node_id, name, params, origin=origin)
            except ControlError as e:
                raise RequestError(str(e)) from None
        return {**response, "result": result, "mode": mode}

    # ================= ПЕСОЧНИЦА =================

    @staticmethod
    def _sender(session: Session) -> dict[str, Any]:
        if session.role == ROLE_ADMIN:
            return {"role": "operator", "team": None}   # админка — рабочее место Оператора купола
        return {"role": session.role, "team": session.team}

    async def _meeting_control(self, session: Session, action: str, value: Any) -> dict[str, Any]:
        canonical = MeetingRoom.resolve_action(action)
        if canonical is None or not self.policy.can_control(session.role, MEETING_NODE, canonical):
            raise RequestError(f"Действие {action!r} для {MEETING_NODE} недоступно. "
                               f"Разрешено: {self._allowed_controls(session, MEETING_NODE) or 'ничего'}")
        if canonical == "clear":
            removed = self.meeting.clear()
            logger.info("%s cleared meeting history (%d messages)", session, removed)
            self._broadcast_meeting({"type": "meeting_cleared"})
            return {"success": True, "node_id": MEETING_NODE, "action": action, "removed": removed}

        now = time.monotonic()
        limit, window = MEETING_RATE
        while session.posts and now - session.posts[0] > window:
            session.posts.popleft()
        if len(session.posts) >= limit:
            raise RequestError(f"Слишком часто: не больше {limit} сообщений за {window:.0f} с")
        try:
            message = self.meeting.post(self._sender(session), value)
        except MeetingError as e:
            raise RequestError(str(e)) from None
        session.posts.append(now)
        logger.info("%s meeting #%d: %s", session, message["id"], _short(message["text"]))
        self._log_message(message)
        if session.role in (ROLE_ADMIN, ROLE_TEAM):   # гости (неактивные команды) на смену не влияют
            origin = "operator" if session.role == ROLE_ADMIN else "agent"
            await asyncio.to_thread(self.simulator.record_chat, message, origin)
        self._broadcast_meeting({"type": "meeting_message", "message": message})
        return {"success": True, "node_id": MEETING_NODE, "action": action, "message": message}

    async def _operator_loop(self) -> None:
        """Публикует в песочнице реплики виртуального Оператора сценария смены."""
        while True:
            try:
                while True:
                    line = self._operator_lines.get_nowait()
                    value = {"text": line.get("text"), "name": "Оператор", "to": line.get("to")}
                    message = self.meeting.post({"role": "operator", "team": None}, value)
                    logger.info("operator (сценарий) meeting #%d: %s", message["id"], _short(message["text"]))
                    self._log_message(message)
                    self._broadcast_meeting({"type": "meeting_message", "message": message})
            except queue.Empty:
                pass
            except MeetingError as e:
                logger.error("Реплика Оператора не опубликована: %s", e)
            await asyncio.sleep(0.5)

    def _broadcast_meeting(self, payload: dict[str, Any]) -> None:
        for s in list(self.sessions):
            if self.policy.node_visible(s.role, MEETING_NODE):
                asyncio.create_task(self._send_quietly(s, payload))

    @staticmethod
    async def _send_quietly(session: Session, payload: dict[str, Any]) -> None:
        try:
            await session.send(payload)
        except ConnectionClosed:
            pass

    def _origin(self, session: Session, node_id: str, canonical: str) -> str:
        """Участник — ИИ-агент; админ выполняет и действия Оператора (учитываются в метриках смены)."""
        if session.role != ROLE_ADMIN:
            return "agent"
        info = next((n for n in self.simulator.describe_nodes() if n["node_id"] == node_id), {})
        return "operator" if canonical in info.get("operator_controls", ()) else "admin"

    def _node_controls(self, node_id: str) -> list[str]:
        if node_id == MEETING_NODE:
            return MeetingRoom.describe()["controls"]
        return next(n["controls"] for n in self.simulator.describe_nodes() if n["node_id"] == node_id)

    def _allowed_controls(self, session: Session, node_id: str) -> list[str]:
        return self.policy.allowed_actions(session.role, node_id, self._node_controls(node_id))

    async def handle_describe_nodes(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        nodes = []
        for info in [*self.simulator.describe_nodes(), MeetingRoom.describe()]:
            node_id = info["node_id"]
            if not self.policy.node_visible(session.role, node_id):
                continue
            if session.role == ROLE_ADMIN:
                adapter = self.adapters.get(node_id)
                info["mode"] = self.node_modes.get(node_id, "virtual")
                info["real_device"] = adapter.status() if adapter else None
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

    async def handle_list_crises(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "crises": self.simulator.crisis_catalog(),
                "active": self.simulator.active_crises(), "reports": self.simulator.crisis_reports()}

    async def handle_start_shift(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        """{"params": {...}} — сброс мира и запуск сценария 90-минутной смены (одинаковый для
        всех команд). С реальным Оператором: params.simulate_operator = false."""
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise RequestError("params: ожидался объект")
        if TIME_MODE == "real" and "start_hour" not in params:
            # реальное время: смена начинается с текущего часа купола, часы не перескакивают на 13:30
            params = {**params, "start_hour": round(self.simulator.environment.outdoor.hour, 4)}
        try:
            self.simulator.start_shift(params=params)
        except (KeyError, ValueError) as e:
            raise RequestError(str(e)) from None
        logger.info("%s started shift (params=%s)", session, params)
        return {"success": True}

    async def handle_shift_report(self, session: Session, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "report": self.simulator.shift_report()}

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
        # участники прежней команды и гости новой — переподключаются с новой ролью
        revoked = [s for s in self.sessions if (s.role == ROLE_TEAM and s.team != team)
                   or (s.role == ROLE_GUEST and s.team == team)]
        for s in revoked:
            asyncio.create_task(self._revoke(s, f"Активная команда сменилась на {team} — переподключитесь"))
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
        if node_id not in (*self.simulator.node_ids(), MEETING_NODE):
            raise RequestError(f"Неизвестный узел: {node_id!r}")
        changes = {k: data[k] for k in ("actions", "visible", "hidden_params") if k in data}
        if not changes:
            raise RequestError("Нужно указать хотя бы одно из: actions, visible, hidden_params")
        if isinstance(changes.get("actions"), list):
            valid = set(self._node_controls(node_id))
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
        role, guest_index = None, None
        if isinstance(data, dict) and data.get("type") == "auth":
            role = self.protector.getRole(data.get("key"))
            if role is None:   # ключ неактивной команды — доступ только к песочнице
                guest_index = self.protector.getTeamIndex(data.get("key"))
                role = ROLE_GUEST if guest_index is not None else None
        if role is None:
            logger.warning("Отказ в авторизации: %s", websocket.remote_address)
            await asyncio.sleep(1.0)  # замедляем перебор ключей
            await websocket.send(json.dumps({"type": "auth_response", "request_id": request_id,
                                             "success": False, "error": "Неверный ключ доступа"}))
            await websocket.close(CLOSE_UNAUTHORIZED, "unauthorized")
            return None

        team = (self.protector.getActiveTeamIndex() or 0) + 1 if role == ROLE_TEAM else (
            guest_index + 1 if role == ROLE_GUEST else None)
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
        if msg_type == "control" and entry is not None and session.role in entry[1]:
            try:
                self._log_control(session, data, response)
            except Exception:
                logger.exception("Журнал участника: ошибка записи команды")
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


def _start_hour() -> float:
    raw = START_HOUR or ("now" if TIME_MODE == "real" else "8")
    if raw == "now":
        now = time.localtime()
        return now.tm_hour + now.tm_min / 60.0 + now.tm_sec / 3600.0
    hour = float(raw)
    if not 0.0 <= hour < 24.0:
        raise ValueError(f"DOME_START_HOUR: ожидался час 0..24 или now, получено {raw!r}")
    return hour


def _short(value: Any, limit: int = 120) -> str:
    """Для лога: длинные значения (G-code в upload_file, сообщения чата) обрезаются."""
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        value = {**value, "content": f"<{len(value['content'])} символов>"}
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ================= ЗАПУСК =================

async def main() -> None:
    if TIME_MODE not in DAY_LENGTH_S:
        logger.error("DOME_TIME_MODE: ожидалось hybrid или real, получено %r", TIME_MODE)
        return
    try:
        _start_hour()
    except ValueError as e:
        logger.error("%s", e)
        return
    try:
        server = DomeServer()
    except (FileNotFoundError, ValueError) as e:
        logger.error("Авторизация не настроена: %s", e)
        logger.error("Создайте ключи: python authorization/generate_keys.py")
        return

    await server.start()
    try:
        async with serve(server.handle_client, HOST, PORT, max_size=MAX_MESSAGE):
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

"""Событийный оркестратор общего чата ИИ-агентов."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import defaultdict
from typing import Any, DefaultDict, List, Set
from chat_bus import ChatMessage, SharedChatLog
from relevance import RelevanceScorer

logger = logging.getLogger(__name__)

TICK_SLEEP_SEC = 0.25
MAX_CHAIN_DEPTH = 3
MAX_REPLIES_PER_ROOT = 8


class Orchestrator:
    def __init__(self, agents: List[Any], bus: SharedChatLog, scorer: RelevanceScorer):
        self._agents = agents
        self._agents_by_name = {agent.config.name: agent for agent in agents}
        self._bus = bus
        self._scorer = scorer
        self._processed_index = 0
        self._operator_queue: "queue.Queue[str]" = queue.Queue()
        self._stop_event = threading.Event()

        # Для одной исходной цепочки каждый агент отвечает не более одного раза.
        self._responded_by_root: DefaultDict[str, Set[str]] = defaultdict(set)
        self._reply_count_by_root: DefaultDict[str, int] = defaultdict(int)

    def submit_operator_message(self, text: str) -> None:
        text = text.strip()
        if text:
            self._operator_queue.put(text)

    def run(self) -> None:
        logger.info("Оркестратор запущен")
        try:
            while not self._stop_event.is_set():
                self.tick()
                self._stop_event.wait(TICK_SLEEP_SEC)
        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C")

    def tick(self) -> None:
        self._drain_operator_queue()
        self._process_new_messages()

    def stop(self) -> None:
        self._stop_event.set()

    def _drain_operator_queue(self) -> None:
        while True:
            try:
                text = self._operator_queue.get_nowait()
            except queue.Empty:
                return
            self._bus.append("Оператор", text)

    def _process_new_messages(self) -> None:
        new_messages = self._bus.since(self._processed_index)
        self._processed_index += len(new_messages)

        for message in new_messages:
            self._process_message(message)

    def _process_message(self, message: ChatMessage) -> None:
        root_id = message.root_id or message.message_id
        if message.depth >= MAX_CHAIN_DEPTH:
            return
        if self._reply_count_by_root[root_id] >= MAX_REPLIES_PER_ROOT:
            return

        relevant = self._scorer.select_relevant(
            message.content,
            [agent.config for agent in self._agents],
            exclude_name=message.speaker,
            is_event=message.is_event,
        )

        for agent_config in relevant:
            if self._reply_count_by_root[root_id] >= MAX_REPLIES_PER_ROOT:
                break
            if agent_config.name in self._responded_by_root[root_id]:
                continue

            agent = self._agents_by_name[agent_config.name]
            if time.time() - agent.last_spoken_ts < agent.config.cooldown_sec:
                continue

            self._responded_by_root[root_id].add(agent_config.name)
            try:
                agent.respond(cause=message)
            except Exception as error:
                logger.exception("Ошибка ответа агента %s: %s", agent_config.name, error)
                self._bus.append(
                    "Система",
                    f"Агент «{agent_config.name}» временно недоступен: {error}",
                    root_id=root_id,
                    depth=message.depth + 1,
                )
            finally:
                agent.last_spoken_ts = time.time()
                self._reply_count_by_root[root_id] += 1

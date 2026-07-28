"""
Главный цикл: следит за новыми сообщениями в общем чате. Для каждого нового
сообщения через RelevanceScorer определяет, кто из агентов должен ответить,
и по очереди даёт им слово. Порядок реплик внутри одного "тика" — просто
порядок обхода списка агентов, но САМ факт участия — событийный/эвристический
(по ключевым словам и флагу always_relevant_on_crisis), как требует ТЗ.
"""

import logging
import queue
import threading
import time
from typing import List

from agent import Agent
from chat_bus import SharedChatLog
from relevance import RelevanceScorer

logger = logging.getLogger(__name__)

MAX_CASCADE_REPLIES_PER_TICK = 6   # защита от чрезмерно длинной цепочки реплик за один тик
TICK_SLEEP_SEC = 0.3


class Orchestrator:
    def __init__(self, agents: List[Agent], bus: SharedChatLog, scorer: RelevanceScorer):
        self._agents = agents
        self._agents_by_name = {a.config.name: a for a in agents}
        self._bus = bus
        self._scorer = scorer
        self._processed_index = 0
        self._operator_queue: "queue.Queue[str]" = queue.Queue()
        self._stop_event = threading.Event()

    def submit_operator_message(self, text: str) -> None:
        """Добавить реплику оператора (вызывается из потока чтения stdin)."""
        self._operator_queue.put(text)

    def run(self) -> None:
        logger.info("Оркестратор запущен. Ctrl+C для остановки.")
        try:
            while not self._stop_event.is_set():
                self._drain_operator_queue()
                self._process_new_messages()
                time.sleep(TICK_SLEEP_SEC)
        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C")

    def stop(self) -> None:
        self._stop_event.set()

    def _drain_operator_queue(self) -> None:
        while not self._operator_queue.empty():
            text = self._operator_queue.get_nowait()
            self._bus.append(speaker="Оператор", content=text, is_event=False)

    def _process_new_messages(self) -> None:
        new_messages = self._bus.since(self._processed_index)
        self._processed_index += len(new_messages)

        cascade_budget = MAX_CASCADE_REPLIES_PER_TICK
        for msg in new_messages:
            if cascade_budget <= 0:
                break
            if msg.speaker in self._agents_by_name:
                # Реплики самих агентов не разбираем здесь повторно —
                # они станут "новыми сообщениями" на следующем тике,
                # что и даёт контролируемый каскад реакций между агентами.
                continue

            relevant = self._scorer.select_relevant(
                msg.content,
                [a.config for a in self._agents],
                exclude_name=msg.speaker,
                is_event=msg.is_event,
            )

            for agent_config in relevant:
                if cascade_budget <= 0:
                    break
                agent = self._agents_by_name[agent_config.name]
                if time.time() - agent.last_spoken_ts < agent.config.cooldown_sec:
                    continue  # агент недавно уже говорил — даём ему остыть

                reply = agent.respond()
                agent.last_spoken_ts = time.time()
                cascade_budget -= 1
                print(f"\n[{agent.config.name}]: {reply}")

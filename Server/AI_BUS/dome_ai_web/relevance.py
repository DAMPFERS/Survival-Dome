"""
Определение релевантности: должен ли конкретный агент отреагировать на новое сообщение.

Реализация — простой keyword-scorer (быстро, бесплатно, без доп. вызовов LLM).
Интерфейс намеренно узкий (текст + агент -> bool/score), чтобы в будущем его
можно было заменить на embedding-based scorer (например, косинусное сходство
через sentence-transformers) без изменения оркестратора — просто передайте
другую реализацию в Orchestrator.
"""

from typing import List

from config import AgentConfig


class RelevanceScorer:
    def __init__(self, threshold: int = 1):
        self._threshold = threshold

    def score(self, text: str, agent: AgentConfig) -> int:
        text_lower = text.lower()
        return sum(1 for kw in agent.topic_keywords if kw in text_lower)

    def is_relevant(self, text: str, agent: AgentConfig, is_event: bool = False) -> bool:
        if is_event and agent.always_relevant_on_crisis:
            return True
        return self.score(text, agent) >= self._threshold

    def select_relevant(self, text: str, agents: List[AgentConfig],
                         exclude_name: str = None, is_event: bool = False) -> List[AgentConfig]:
        """Возвращает список конфигов агентов, которым стоит вступить в разговор."""
        result = []
        for agent in agents:
            if agent.name == exclude_name:
                continue
            if self.is_relevant(text, agent, is_event=is_event):
                result.append(agent)
        return result

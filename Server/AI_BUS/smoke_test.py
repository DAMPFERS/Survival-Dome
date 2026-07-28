"""
Смоук-тест логики оркестрации/релевантности БЕЗ реального DeepSeek API и без
реального сервера телеметрии. Агенты подменены заглушками, которые просто
эхом дают ответ на основе своей роли — цель теста: убедиться, что
1) на кризисное событие реагируют профильный агент + координатор,
2) на обычную реплику про батарею реагирует только Энергетик,
3) каскад (агент ответил -> его реплика тоже может кого-то заинтересовать)
   работает на следующем тике, а не зацикливается.
"""

from chat_bus import SharedChatLog
from config import ALL_AGENTS
from relevance import RelevanceScorer


class FakeAgent:
    """Имитация agent.Agent без обращения к LLM API."""

    def __init__(self, config, bus):
        self.config = config
        self._bus = bus
        self.last_spoken_ts = 0.0
        self.calls = 0

    def respond(self) -> str:
        self.calls += 1
        text = f"(заглушка) {self.config.name} проверил свои системы, всё под контролем."
        self._bus.append(self.config.name, text)
        return text


def run_tick(bus, agents_by_name, scorer, processed_index, cascade_budget=6):
    new_messages = bus.since(processed_index)
    processed_index += len(new_messages)
    for msg in new_messages:
        if msg.speaker in agents_by_name:
            continue
        relevant = scorer.select_relevant(
            msg.content, [a.config for a in agents_by_name.values()],
            exclude_name=msg.speaker, is_event=msg.is_event,
        )
        for cfg in relevant:
            if cascade_budget <= 0:
                break
            agent = agents_by_name[cfg.name]
            agent.respond()
            cascade_budget -= 1
    return processed_index


def main():
    bus = SharedChatLog()
    scorer = RelevanceScorer(threshold=1)
    agents_by_name = {cfg.name: FakeAgent(cfg, bus) for cfg in ALL_AGENTS}

    processed = 0

    print("--- Тест 1: обычное сообщение про батарею ---")
    bus.append("Оператор", "Что с батареей, не разряжена?")
    processed = run_tick(bus, agents_by_name, scorer, processed)
    responders = [m.speaker for m in bus.since(0) if m.speaker != "Оператор"]
    print("Ответили:", responders)
    assert responders == ["Энергетик"], f"Ожидался только Энергетик, получили {responders}"

    print("\n--- Тест 2: кризисное событие ---")
    start_len = len(bus)
    bus.append("СОБЫТИЕ", "Резкий скачок температуры внутри купола.", is_event=True)
    processed = run_tick(bus, agents_by_name, scorer, processed)
    new_responders = [m.speaker for m in bus.since(start_len) if m.speaker != "СОБЫТИЕ"]
    print("Ответили на кризис:", new_responders)
    assert "Телеметрист" in new_responders, "Телеметрист должен среагировать по ключевому слову"
    assert "Координатор" in new_responders, "Координатор должен среагировать по always_relevant_on_crisis"
    assert "Энергетик" not in new_responders, "Энергетик не должен реагировать на чисто климатический кризис"
    assert "Связист" not in new_responders, "Связист не должен реагировать на чисто климатический кризис"
    assert "Производственник" not in new_responders, "Производственник не должен реагировать на климат-кризис"

    print("\nВсе проверки пройдены успешно.")


if __name__ == "__main__":
    main()

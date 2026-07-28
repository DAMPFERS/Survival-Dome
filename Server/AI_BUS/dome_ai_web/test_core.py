from types import SimpleNamespace
from chat_bus import SharedChatLog
from orchestrator import Orchestrator
from relevance import RelevanceScorer

class FakeAgent:
    def __init__(self,name,keywords,always=False):
        self.config=SimpleNamespace(name=name,topic_keywords=keywords,always_relevant_on_crisis=always,cooldown_sec=0)
        self.last_spoken_ts=0
        self.bus=None
    def respond(self,cause=None):
        text=f"{self.config.name}: реакция на {cause.speaker}"
        self.bus.append(self.config.name,text,root_id=cause.root_id,depth=cause.depth+1)
        return text

def run():
    bus=SharedChatLog(); agents=[FakeAgent('Энергетик',['батаре','энерг']),FakeAgent('Координатор',['реакция'],True)]
    for a in agents:a.bus=bus
    o=Orchestrator(agents,bus,RelevanceScorer(1))
    bus.append('Оператор','Проверь батарею')
    for _ in range(6):o.tick()
    msgs=bus.since(0)
    assert [m.speaker for m in msgs].count('Энергетик')==1
    assert [m.speaker for m in msgs].count('Координатор')==1
    assert max(m.depth for m in msgs)<=3
    print('OK:',[(m.speaker,m.depth) for m in msgs])
if __name__=='__main__':run()

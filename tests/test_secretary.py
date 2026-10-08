from pa.agents.secretary import ask
from pa.llm.router import LLMResult


class StubRouter:
    def __init__(self, data, degraded=False):
        self.data, self.degraded, self.calls = data, degraded, []

    async def run(self, task, system_name, user_text, schema, *, user_initiated=False):
        self.calls.append((task, system_name, user_text, user_initiated))
        return LLMResult(self.data, "ai", self.degraded)


async def test_ask_returns_reply():
    router = StubRouter({"reply": "이번 주 일정은 두 건입니다.", "notes": ""})
    reply, degraded = await ask(router, "이번 주 일정 알려줘")
    assert reply == "이번 주 일정은 두 건입니다." and not degraded
    assert router.calls[0][0] == "secretary_chat" and router.calls[0][3] is True


async def test_ask_degraded_returns_none():
    reply, degraded = await ask(StubRouter(None, degraded=True), "x")
    assert reply is None and degraded

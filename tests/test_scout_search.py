from pathlib import Path
from pa.llm.budget import Budget
from pa.llm.router import TaskRouter
from pa.scout.search import scout_search


class FakeAI:
    def __init__(self, data=None, exc=None):
        self.data, self.exc, self.calls = data, exc, []

    async def run_json(self, prompt, system_file, allowed_tools=""):
        self.calls.append(allowed_tools)
        if self.exc:
            raise self.exc
        return self.data, {"input_tokens": 1, "output_tokens": 1}


def _router(con, ai, per_day_web=6):
    b = Budget(con, per_5h=25, reserve_for_user=8, per_day_web=per_day_web)
    return TaskRouter(con, ai, b, Path("config/prompts"))


async def test_uses_web_tools_and_returns_items(con):
    c = FakeAI({"items": [{"title": "숲길 산책", "url": "https://example.com/a"}, {"title": "x", "url": 3}]})
    r = await scout_search(_router(con, c), "outing", "아이와 주말 산책", "아직 학습된 취향 없음", user_initiated=True)
    assert c.calls == ["WebSearch"]
    assert [i["url"] for i in r.items] == ["https://example.com/a"] and r.degraded is False
    assert con.execute("SELECT tier FROM llm_call").fetchone()["tier"] == "ai_web"


async def test_error_degrades_without_items(con):
    r = await scout_search(_router(con, FakeAI(exc=RuntimeError("x"))), "outing", "q", "", user_initiated=True)
    assert r.items == [] and r.degraded and r.reason == "error"


async def test_daily_web_budget(con):
    c = FakeAI({"items": []})
    rt = _router(con, c, per_day_web=1)
    await scout_search(rt, "outing", "q1", "", user_initiated=True)
    r = await scout_search(rt, "outing", "q2", "", user_initiated=True)
    assert r.reason == "budget" and len(c.calls) == 1

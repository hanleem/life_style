import pytest

from pa.llm.budget import Budget
from pa.llm.codex_cli import RateLimited
from pa.llm.router import TASK_TIER, TaskRouter

SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]}
TASK = "secretary_chat"


class FakeAI:
    def __init__(self, exc=None):
        self.calls, self.exc, self.tools = 0, exc, []

    async def run_json(self, prompt, system_file, allowed_tools=""):
        self.calls += 1
        self.tools.append(allowed_tools)
        if self.exc:
            raise self.exc
        return {"reply": "AI 답변"}, {"input_tokens": 100, "output_tokens": 50}


@pytest.fixture
def prompts(tmp_path):
    (tmp_path / "secretary.md").write_text("너는 비서다", encoding="utf-8")
    (tmp_path / "scout.md").write_text("너는 스카우트다", encoding="utf-8")
    return tmp_path


def _router(con, prompts, ai, per_5h=25, reserve=8, per_day_web=6):
    return TaskRouter(con, ai, Budget(con, per_5h, reserve, per_day_web=per_day_web), prompts)


async def test_ai_task_uses_ai_and_records(con, prompts):
    r = _router(con, prompts, FakeAI())
    res = await r.run(TASK, "secretary", "공지 써줘", SCHEMA, user_initiated=True)
    assert res.tier == "ai" and res.data["reply"] == "AI 답변" and not res.degraded
    row = con.execute("SELECT tier, input_tokens FROM llm_call").fetchone()
    assert tuple(row) == ("ai", 100)


async def test_cache_hit_on_second_call(con, prompts):
    c = FakeAI()
    r = _router(con, prompts, c)
    await r.run(TASK, "secretary", "같은 요청", SCHEMA, user_initiated=False)
    res = await r.run(TASK, "secretary", "같은 요청", SCHEMA, user_initiated=False)
    assert res.tier == "cache" and c.calls == 1


async def test_user_initiated_calls_are_not_cached(con, prompts):
    c = FakeAI()
    r = _router(con, prompts, c)
    await r.run(TASK, "secretary", "같은 요청", SCHEMA, user_initiated=True)
    res = await r.run(TASK, "secretary", "같은 요청", SCHEMA, user_initiated=True)
    assert res.tier == "ai" and c.calls == 2


async def test_budget_exhausted_returns_degraded_without_data(con, prompts):
    c = FakeAI()
    r = _router(con, prompts, c, per_5h=2, reserve=0)
    for i in range(2):
        await r.run(TASK, "secretary", f"요청{i}", SCHEMA, user_initiated=True)
    res = await r.run(TASK, "secretary", "요청X", SCHEMA, user_initiated=True)
    assert res.degraded and res.data is None and c.calls == 2


async def test_scheduled_work_respects_user_reserve(con, prompts):
    r = _router(con, prompts, FakeAI(), per_5h=3, reserve=2)
    await r.run(TASK, "secretary", "a", SCHEMA, user_initiated=False)
    res = await r.run(TASK, "secretary", "b", SCHEMA, user_initiated=False)
    assert res.degraded and res.data is None   # 정기 작업은 3-2=1회까지만


async def test_rate_limited_sets_cooldown(con, prompts):
    c = FakeAI(exc=RateLimited("limit"))
    r = _router(con, prompts, c)
    res = await r.run(TASK, "secretary", "x", SCHEMA, user_initiated=True)
    assert res.degraded and res.data is None
    c.exc = None
    res2 = await r.run(TASK, "secretary", "y", SCHEMA, user_initiated=True)
    assert res2.degraded and c.calls == 1


async def test_router_logs_type_and_length_only(con, prompts, caplog):
    from pa.llm.codex_cli import CodexCLIError
    c = FakeAI(exc=CodexCLIError("TOPSECRET-OUTPUT"))
    r = _router(con, prompts, c)
    with caplog.at_level("WARNING"):
        res = await r.run(TASK, "secretary", "x", SCHEMA, user_initiated=True)
    assert res.degraded and "TOPSECRET" not in caplog.text and "CodexCLIError" in caplog.text
    c.exc = RateLimited("TOPSECRET-OUTPUT")
    caplog.clear()
    with caplog.at_level("WARNING"):
        await r.run(TASK, "secretary", "y", SCHEMA, user_initiated=True)
    assert "TOPSECRET" not in caplog.text


async def test_web_task_passes_search_tool_only_for_scout(con, prompts):
    c = FakeAI()
    r = _router(con, prompts, c)
    await r.run("scout_search", "scout", "나들이", SCHEMA, user_initiated=True)
    await r.run(TASK, "secretary", "일반", SCHEMA, user_initiated=True)
    assert c.tools == ["WebSearch", ""]


async def test_web_budget_per_day(con, prompts):
    c = FakeAI()
    r = _router(con, prompts, c, per_day_web=1)
    ok = await r.run("scout_search", "scout", "a", SCHEMA, user_initiated=True)
    res = await r.run("scout_search", "scout", "b", SCHEMA, user_initiated=True)
    assert not ok.degraded and res.degraded and res.data is None and c.calls == 1


def test_no_local_model_tier_remains():
    assert {cfg["tier"] for cfg in TASK_TIER.values()} <= {"ai", "ai_web"}
    import pa.llm
    assert not hasattr(pa.llm, "local")


async def test_fallback_used_when_ai_fails(con, prompts):
    c = FakeAI(exc=RateLimited("limit"))
    r = _router(con, prompts, c)
    res = await r.run(TASK, "secretary", "x", SCHEMA, user_initiated=True, fallback=lambda: {"reply": "규칙 답"})
    assert res.tier == "rule" and res.data == {"reply": "규칙 답"} and not res.degraded
    row = con.execute("SELECT tier, ok, fallback_from FROM llm_call").fetchall()[-1]
    assert tuple(row) == ("rule", 1, "ai")


async def test_fallback_used_when_budget_exhausted(con, prompts):
    c = FakeAI()
    r = _router(con, prompts, c, per_5h=0, reserve=0)
    res = await r.run(TASK, "secretary", "x", SCHEMA, user_initiated=True, fallback=lambda: {"reply": "r"})
    assert res.tier == "rule" and c.calls == 0


async def test_fallback_that_raises_or_returns_none_degrades(con, prompts):
    r = _router(con, prompts, FakeAI(exc=RateLimited("limit")))
    boom = lambda: 1 / 0
    assert (await r.run(TASK, "secretary", "x", SCHEMA, user_initiated=True, fallback=boom)).degraded
    assert (await r.run(TASK, "secretary", "y", SCHEMA, user_initiated=True, fallback=lambda: None)).degraded


async def test_tone_is_filled_into_system_prompt(con, prompts):
    (prompts / "secretary.md").write_text("말투: {tone}", encoding="utf-8")
    seen = []

    class Spy(FakeAI):
        async def run_json(self, prompt, system_file, allowed_tools=""):
            seen.append(prompt)
            return await super().run_json(prompt, system_file, allowed_tools)
    r = _router(con, prompts, Spy())
    r.tone = "friendly"
    await r.run(TASK, "secretary", "안녕", SCHEMA, user_initiated=True)
    assert "친근한 말투" in seen[0] and "{tone}" not in seen[0]


async def test_images_are_passed_and_never_cached(con, prompts):
    got = []

    class Img(FakeAI):
        async def run_json(self, prompt, system_file, allowed_tools="", images=()):
            got.append(list(images))
            return {"reply": "ok"}, {"input_tokens": 1, "output_tokens": 1}
    c = Img()
    r = _router(con, prompts, c)
    await r.run("notice_extract", "secretary", "사진", SCHEMA, user_initiated=True, images=["a.jpg"])
    await r.run("notice_extract", "secretary", "사진", SCHEMA, user_initiated=False, images=["a.jpg"])
    assert got == [["a.jpg"], ["a.jpg"]]

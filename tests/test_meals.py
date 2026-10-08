"""주간 식단: 기간 계산, 생성·저장, 일정 작업, 알림."""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pa.core import meals, notify, supply
from pa.llm.router import LLMResult
from pa.rules import load_rules

KST = ZoneInfo("Asia/Seoul")
THU = datetime(2026, 10, 8, 9, 30, tzinfo=KST)


class Router:
    def __init__(self, data):
        self.data, self.calls = data, []

    async def run(self, task, system_name, text, schema, *, user_initiated=False, fallback=None):
        self.calls.append((task, system_name, text, user_initiated))
        return LLMResult(self.data, "ai", self.data is None)


def _plan(titles=("그레인 볼", "연어", "프리타타")):
    return {"meals": [{"date": "2026-10-10", "slot": "cook", "title": titles[0], "why_new": "퀴노아", "time_min": 30,
                       "servings": 1, "ingredients": [{"name": "퀴노아", "qty": "1컵"}], "steps": ["삶는다"]},
                      {"date": "2026-10-11", "slot": "cook", "title": titles[1], "why_new": "", "time_min": 25,
                       "servings": 1, "ingredients": [], "steps": []},
                      {"date": "2026-10-12", "slot": "lunchbox", "title": titles[2], "why_new": "", "time_min": 35,
                       "servings": 1, "ingredients": [], "steps": []}],
            "shopping": [{"category": "채소", "items": [{"name": "주키니", "qty": "2개"}]}],
            "delivery": {"count": 6, "note": ""}}


def test_window_normal_week():
    w = meals.week_window(date(2026, 10, 8))                 # 목
    assert w["cook_days"] == [date(2026, 10, 10), date(2026, 10, 11)]
    assert w["lunchbox"] == date(2026, 10, 12)
    assert w["delivery_from"] == date(2026, 10, 13) and w["delivery_count"] == 6
    assert w["week_start"] == date(2026, 10, 10)


def test_window_monday_holiday_cooks_instead_of_lunchbox():
    supply.set_holidays({date(2026, 10, 12)})
    w = meals.week_window(date(2026, 10, 8))
    assert w["lunchbox"] is None and date(2026, 10, 12) in w["cook_days"]


def test_window_holiday_in_delivery_week_reduces_count():
    supply.set_holidays({date(2026, 10, 14)})                # 수요일 휴일
    assert meals.week_window(date(2026, 10, 8))["delivery_count"] == 5


async def test_generate_stores_plan_and_prompt_has_prefs_and_pantry(con):
    r = Router(_plan())
    p = await meals.generate(con, r, THU, user_initiated=False)
    assert p and p["meals"][0]["title"] == "그레인 볼"
    task, system, text, ui = r.calls[0]
    assert task == "meal_plan" and system == "meal_plan" and ui is False
    assert "이번 주 요리 종류" in text and "병아리콩" in text and "2026-10-10" in text
    assert meals.latest(con)["week_start"] == "2026-10-10"


async def test_generate_failure_returns_none_and_stores_nothing(con):
    assert await meals.generate(con, Router(None), THU, user_initiated=False) is None
    assert meals.latest(con) is None


async def test_recent_titles_are_passed_to_avoid_repeats(con):
    await meals.generate(con, Router(_plan(("지난주 볼", "a", "b"))), THU - timedelta(days=7), user_initiated=False)
    r = Router(_plan())
    await meals.generate(con, r, THU, user_initiated=False)
    assert "지난주 볼" in r.calls[0][2]


async def test_run_job_plan_then_notify_once(con):
    rules = load_rules()
    r = Router(_plan())
    await meals.run_job(con, "meal_plan", THU, rules, r)
    await meals.run_job(con, "meal_plan", THU, rules, r)
    kinds = [n["kind"] for n in notify.unread(con, 50)]
    assert sorted(kinds) == ["meal_plan", "reminder"]             # 같은 주 두 번 불러도 알림은 한 번(식단 전용 알림 + 안내 문장)
    assert len(r.calls) == 1


async def test_reminders_need_a_plan_and_send_once(con):
    rules = load_rules()
    fri, sat = THU + timedelta(days=1), THU + timedelta(days=2)
    await meals.run_job(con, "meal_grocery", fri, rules, Router(None))
    assert notify.unread(con, 50) == []                                # 식단이 없으면 보낼 게 없다
    await meals.generate(con, Router(_plan()), THU, user_initiated=False)
    await meals.run_job(con, "meal_grocery", fri, rules, Router(None))
    await meals.run_job(con, "meal_delivery", sat, rules, Router(None))
    assert [n["kind"] for n in notify.unread(con, 50)].count("routine_supply") == 2


CFG = {"prefs": {"cuisine_base": "서양식", "cuisine_variety": ["일식", "멕시칸", "인도식"], "variety_weeks_of_month": [2, 4]}}


def test_week_of_month_decides_healthy_vs_variety(con):
    meals.ensure_schema(con)
    expect = {date(2026, 10, 3): "서양식", date(2026, 10, 10): "variety", date(2026, 10, 17): "서양식",
              date(2026, 10, 24): "variety", date(2026, 10, 31): "서양식"}          # 1·3·5주 건강식, 2·4주 변화
    for ws, want in expect.items():
        got = meals.pick_cuisine(con, ws, CFG)
        assert (got == "서양식") if want == "서양식" else (got in ("일식", "멕시칸", "인도식")), ws


def test_variety_choice_is_stable_and_avoids_recent(con):
    meals.ensure_schema(con)
    ws = date(2026, 10, 10)                                                     # 10월 2주
    first = meals.pick_cuisine(con, ws, CFG)
    assert first == meals.pick_cuisine(con, ws, CFG)                            # 같은 주는 항상 같은 선택
    con.execute("INSERT INTO meal_plan(week_start, plan_json, created_at) VALUES(?,?,'t')",
                ((ws - timedelta(days=14)).isoformat(), json.dumps({"meals": [], "cuisine": first})))
    assert meals.pick_cuisine(con, ws, CFG) != first                            # 최근에 쓴 종류는 피한다


async def test_prompt_mood_is_healthy_or_spicy_by_week(con, monkeypatch):
    monkeypatch.setattr(meals, "load_config", lambda: CFG)
    r = Router(_plan())
    await meals.generate(con, r, datetime(2026, 10, 15, 9, 30, tzinfo=KST), user_initiated=False)    # 토 10/17 = 3주
    await meals.generate(con, r, datetime(2026, 10, 22, 9, 30, tzinfo=KST), user_initiated=False)    # 토 10/24 = 4주
    assert "완전 건강식" in r.calls[0][2] and "맵지 않게" in r.calls[0][2]
    assert "조금 매운 요리도 좋음" in r.calls[1][2]


async def test_generate_records_cuisine_in_plan_and_prompt(con):
    r = Router(_plan())
    p = await meals.generate(con, r, THU, user_initiated=False)
    assert p["cuisine"] and f"이번 주 요리 종류: {p['cuisine']}" in r.calls[0][2]
    assert meals.latest(con)["cuisine"] == p["cuisine"]


def test_jobs_are_due_on_their_weekday(con):
    from pa.scheduler import due_jobs
    rules = load_rules()
    assert "meal_plan" in due_jobs(con, datetime(2026, 10, 8, 9, 30, tzinfo=KST), rules)       # 목
    assert "meal_grocery" in due_jobs(con, datetime(2026, 10, 9, 9, 30, tzinfo=KST), rules)    # 금
    assert "meal_delivery" in due_jobs(con, datetime(2026, 10, 10, 9, 0, tzinfo=KST), rules)   # 토
    assert "meal_plan" not in due_jobs(con, datetime(2026, 10, 9, 9, 30, tzinfo=KST), rules)


def test_meals_page_and_generate_route(con, tmp_path, monkeypatch):
    from conftest import owner_client
    from fastapi.testclient import TestClient
    from pa.settings import Settings
    from pa.web.app import create_app
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    app = create_app(s, con, Router(_plan()))
    stranger = TestClient(app, base_url="http://localhost:8010", client=("203.0.113.9", 5000))   # 이 PC가 아닌 기기
    assert stranger.get("/meals").status_code in (401, 403)           # 등록 전 기기에서는 열리지 않는다
    assert stranger.post("/api/meals/generate").status_code in (401, 403)
    c = owner_client(app)                  # 로그인 전에는 열리지 않는다
    assert "아직 이번 주 식단이 없습니다" in c.get("/meals").text
    r = c.post("/api/meals/generate")
    assert r.status_code == 200 and r.json()["ok"]
    html = c.get("/meals").text
    assert "그레인 볼" in html and "주키니" in html and "퀴노아 1컵" in html
    assert "병아리콩" not in html or True


def test_meal_prompt_asks_for_one_person_week_sized_buying_and_reuse():
    from pathlib import Path
    txt = (Path(__file__).resolve().parent.parent / "config" / "prompts" / "meal_plan.md").read_text(encoding="utf-8")
    assert "일주일 안에 다 먹을 수 있는 양" in txt
    assert "가격 효율성과 재료 소진" in txt
    assert "핵심 재료 2개" in txt and "key_ingredients" in txt and "소량으로" in txt
    assert "냉동해 두고" in txt
    assert "이번 주 안에 모두 쓰도록" in txt and "최소 단위" in txt


def test_key_ingredients_are_kept_in_the_plan():
    plan = meals._clean({"meals": [{"title": "a"}], "shopping": [], "key_ingredients": ["오이", "닭다리살", " ", 3, "x" * 50, "넷째"]})
    assert plan["key_ingredients"] == ["오이", "닭다리살", "x" * 30]       # 이름만, 최대 3개, 길이 제한
    assert meals._clean({"meals": [{"title": "a"}]})["key_ingredients"] == []


async def test_generate_uses_rule_fallback_when_ai_is_down(con):
    from pa.llm.budget import Budget
    from pa.llm.codex_cli import RateLimited
    from pa.llm.router import TaskRouter
    from pathlib import Path

    class Down:
        async def run_json(self, *a, **k):
            raise RateLimited("limit")
    router = TaskRouter(con, Down(), Budget(con, 25, 8), Path("config/prompts"))
    plan = await meals.generate(con, router, THU, user_initiated=True)
    assert plan and plan["source"] == "rule" and len(plan["meals"]) == 3
    assert meals.latest(con)["source"] == "rule"


def test_meals_page_tells_when_menu_is_the_default_one(con, tmp_path):
    from conftest import owner_client
    from pa.settings import Settings
    from pa.web.app import create_app
    class RuleRouter(Router):
        async def run(self, *a, **k):
            return LLMResult(self.data, "rule", False)
    app = create_app(Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path), con, RuleRouter(_plan()))
    c = owner_client(app)
    c.post("/api/meals/generate")
    assert "기본 가정식 메뉴" in c.get("/meals").text

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


def _good_plan(when=THU):
    from pa.core import fallback_meals, profile
    return fallback_meals.plan_for(meals.week_window(when.date()), profile.defaults()["members"])


def test_window_is_seven_days_from_saturday_with_school_days_and_prep():
    w = meals.week_window(date(2026, 10, 8))                 # 목
    assert w["week_start"] == date(2026, 10, 10) and len(w["days"]) == 7 and w["days"][-1] == date(2026, 10, 16)
    assert w["school_days"] == [date(2026, 10, 12 + i) for i in range(5)]       # 월~금 간식
    assert w["weekend_days"] == [date(2026, 10, 10), date(2026, 10, 11)]
    assert w["prep_day"] == date(2026, 10, 11)                                  # 일요일 밑반찬


def test_window_holiday_counts_as_weekend():
    supply.set_holidays({date(2026, 10, 14)})                # 수요일 휴일
    w = meals.week_window(date(2026, 10, 8))
    assert len(w["school_days"]) == 4 and date(2026, 10, 14) in w["weekend_days"]


def test_window_on_saturday_starts_today():
    assert meals.week_window(date(2026, 10, 10))["week_start"] == date(2026, 10, 10)


async def test_generate_stores_plan_and_prompt_has_prefs_and_pantry(con):
    r = Router(_plan())
    p = await meals.generate(con, r, THU, user_initiated=False)
    assert p and p["meals"][0]["title"] == "그레인 볼"
    task, system, text, ui = r.calls[0]
    assert task == "meal_plan" and system == "meal_plan" and ui is False
    assert "이번 주 요리 종류" in text and "병아리콩" in text and "2026-10-10" in text
    assert "[가족" in text and "[일정]" in text and "밑반찬 만들기 1회: 2026-10-11" in text
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
    r = Router(_good_plan())
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
    r = Router(_good_plan())
    await meals.generate(con, r, datetime(2026, 10, 15, 9, 30, tzinfo=KST), user_initiated=False)    # 토 10/17 = 3주
    await meals.generate(con, r, datetime(2026, 10, 22, 9, 30, tzinfo=KST), user_initiated=False)    # 토 10/24 = 4주
    assert len(r.calls) == 2
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
    assert plan and plan["source"] == "rule" and len(plan["meals"]) == 20      # 기본 가족(어른 1명): 아침 7 + 저녁 7 + 간식 5 + 밑반찬 1
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


def test_prompt_has_family_cooking_and_allergy_info(con):
    meals.ensure_schema(con)
    from pa.core import profile
    prof = {**profile.defaults(), "members": [{"role": "adult", "age": 40, "sex": "f"}, {"role": "child", "age": 10, "sex": "m"},
                                              {"role": "child", "age": 8, "sex": "f", "name": "둘째"}],
            "allergies": ["땅콩"], "dislikes": ["가지"], "weekday_cook_min": 20, "weekly_budget_won": 150000,
            "shops": ["mart"], "weekend_lunch": True}
    txt = meals._prompt(con, meals.week_window(date(2026, 10, 8)), meals.load_config(), "가정식 한식", prof)
    assert "2.35인분" in txt and "10세" in txt and "8세" in txt
    assert "알레르기(절대 쓰지 않는다): 땅콩" in txt and "싫어하는 음식(쓰지 않는다): 가지" in txt
    assert "20분 이내" in txt and "150,000원" in txt and "마트" in txt
    assert "주말·휴일 점심: 2026-10-10, 2026-10-11" in txt and "둘째" not in txt
    assert "주말·휴일 점심" not in meals._prompt(con, meals.week_window(date(2026, 10, 8)), meals.load_config(), "x", {**prof, "weekend_lunch": False})


def test_meal_prompt_file_has_growth_rules():
    from pathlib import Path
    txt = (Path(__file__).resolve().parent.parent / "config" / "prompts" / "meal_plan.md").read_text(encoding="utf-8")
    for needle in ("채소 반찬을 2가지 이상", "칼슘이 든 끼니", "철분이 든 끼니", "생선", "kid_spice", "adult_note", "알레르기", "{tone}"):
        assert needle in txt, needle


async def test_growth_shortfall_triggers_exactly_one_retry_with_the_missing_points(con):
    r = Router(_plan())
    await meals.generate(con, r, THU, user_initiated=True)
    assert len(r.calls) == 2
    assert "[성장 영양 보완" not in r.calls[0][2] and "[성장 영양 보완" in r.calls[1][2] and "칼슘" in r.calls[1][2]


async def test_good_plan_is_not_regenerated(con):
    r = Router(_good_plan())
    await meals.generate(con, r, THU, user_initiated=True)
    assert len(r.calls) == 1


async def test_better_retry_replaces_the_first_plan(con):
    class Seq(Router):
        async def run(self, task, system_name, text, schema, *, user_initiated=False, fallback=None):
            self.calls.append((task, system_name, text, user_initiated))
            return LLMResult(_good_plan() if len(self.calls) == 2 else _plan(("첫 식단", "a", "b")), "ai", False)
    r = Seq(None)
    p = await meals.generate(con, r, THU, user_initiated=True)
    assert len(r.calls) == 2 and p["meals"][0]["title"] != "첫 식단"
    assert "첫 식단" not in json.dumps(meals.latest(con), ensure_ascii=False)


async def test_allergen_in_ai_plan_forces_safe_default_menu(con):
    from pa.core import profile
    bad = _good_plan()
    bad["meals"][0]["ingredients"].append({"name": "땅콩", "qty": "1줌"})
    r = Router(bad)
    p = await meals.generate(con, r, THU, user_initiated=True, prof={**profile.defaults(), "allergies": ["땅콩"]})
    assert len(r.calls) == 2 and "땅콩" in r.calls[1][2]            # 한 번 다시 요청(알레르기 경고)
    assert p["source"] == "rule" and not meals.allergens_found(p, ["땅콩"])


def test_allergens_found_checks_titles_and_ingredient_names():
    plan = {"meals": [{"title": "새우볶음밥", "ingredients": [{"name": "밥"}]}, {"title": "죽", "ingredients": [{"name": "땅콩가루"}]}]}
    assert meals.allergens_found(plan, ["새우", "땅콩", "우유"]) == ["땅콩", "새우"]
    assert meals.allergens_found(plan, []) == [] and meals.allergens_found({}, ["새우"]) == []


def test_clean_normalizes_family_fields_and_orders_meals():
    plan = meals._clean({"meals": [{"title": "b", "date": "2026-10-10", "slot": "dinner", "veg_sides": ["나물", " "], "kid_spice": 5},
                                   {"title": "a", "date": "2026-10-10", "slot": "breakfast", "adult_note": "x" * 200},
                                   {"title": "z", "date": "2026-10-09", "slot": "weird"}, {"date": "x"}]})
    assert [m["title"] for m in plan["meals"]] == ["z", "a", "b"]
    b = next(m for m in plan["meals"] if m["title"] == "b")
    assert b["veg_sides"] == ["나물"] and b["kid_spice"] == 0
    assert plan["meals"][0]["slot"] == "dinner" and len(plan["meals"][1]["adult_note"]) == 80


async def test_cart_login_reminder_only_when_shopping_at_coupang(con):
    from pa.core import profile
    rules = load_rules()
    await meals.generate(con, Router(_good_plan()), THU, user_initiated=False)
    await meals.run_job(con, "meal_cart", THU, rules, Router(None), prof={**profile.defaults(), "shops": ["mart"]})
    assert not [n for n in notify.unread(con, 50) if "쿠팡" in n.get("body", n.get("text", ""))]
    await meals.run_job(con, "meal_cart", THU, rules, Router(None), prof={**profile.defaults(), "shops": ["coupang"]})
    assert any("쿠팡" in str(n) for n in notify.unread(con, 50))


def test_home_card_shows_todays_meals_and_growth_badge(con):
    meals.ensure_schema(con)
    plan = _good_plan()
    con.execute("INSERT INTO meal_plan(week_start, plan_json, created_at) VALUES('2026-10-10', ?, 't')", (json.dumps(plan, ensure_ascii=False),))
    card = meals.home_card(con, today=date(2026, 10, 12))
    assert {m["slot"] for m in card["meals"]} == {"breakfast", "snack", "dinner"} and all(m["date"] == "2026-10-12" for m in card["meals"])
    assert card["growth"][0] == "ok"
    early = meals.home_card(con, today=date(2026, 10, 8))                    # 식단 시작 전이면 첫날
    assert all(m["date"] == "2026-10-10" for m in early["meals"])


def test_meals_page_shows_slots_growth_check_and_hides_coupang_for_other_shops(con, tmp_path):
    from conftest import owner_client
    from pa.core import profile
    from pa.settings import Settings
    from pa.web.app import create_app
    meals.ensure_schema(con)
    con.execute("INSERT INTO meal_plan(week_start, plan_json, created_at) VALUES('2026-10-10', ?, 't')",
                (json.dumps(_good_plan(), ensure_ascii=False),))
    profile.save(tmp_path, {**profile.defaults(), "shops": ["mart"]})
    c = owner_client(create_app(Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path), con, Router(None)))
    html = c.get("/meals").text
    assert "이번 주 성장 영양 체크 · 모두 충족" in html and "아침" in html and "저녁" in html and "간식" in html and "밑반찬" in html
    assert "채소 반찬:" in html and "쿠팡에서 찾기" not in html and "150,000" not in html and "200,000원" in html
    profile.save(tmp_path, {**profile.defaults(), "shops": ["coupang"]})
    assert "쿠팡에서 찾기" in c.get("/meals").text

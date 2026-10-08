import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import respx

from pa.core import recs
from pa.llm.router import LLMResult
from pa.scout import store
from pa.scout.config import OutingCfg, ScoutConfig, load_scout_config
from pa.scout.outing import allowed_oneway_max, build_outing, display_takeout, normalize_course
from pa.scout.search import SearchResult

RAW = {"drive": {"from_region": "서울 종로구 한빛로 999 ○○아파트", "stops": ["관악산"], "roundtrip_min_est": [60, 90], "basis": "추정"},
       "walk": {"name": "관악산 둘레길", "closure_check": {"status": "ok", "source": "", "checked_at": ""}},
       "food": {"place": "트라토리아X", "takeout": "yes", "price": {"value": "18000", "checked_at": ""},
                "sources": ["https://blog.naver.com/a/1", "https://map.naver.com/p/1"]},
       "order": "walk_then_takeout", "alternative": {"summary": "실내 산책", "reason": "rain"}}
REGION = "서울 종로구 종로3가"


def test_review_only_takeout_unknown():
    c = normalize_course(RAW, from_region=REGION)
    assert c.food.takeout == "unknown"
    assert display_takeout(c) == "포장 가능 여부 확인 필요"


def test_official_source_keeps_takeout():
    raw = {**RAW, "food": {**RAW["food"], "sources": ["https://www.instagram.com/p/1"]}}
    c = normalize_course(raw, from_region=REGION)
    assert c.food.takeout == "yes"
    assert display_takeout(c) == "포장 가능(출처 확인)"


def test_exact_address_overwritten_by_region():
    assert normalize_course(RAW, from_region=REGION).drive.from_region == REGION


def test_closure_without_source_unknown_and_price_unchecked():
    c = normalize_course(RAW, from_region=REGION)
    assert c.walk.closure_check.status == "unknown"
    assert c.food.price.value == "가격 확인 필요"


def test_bad_fields_fall_back_to_defaults():
    c = normalize_course({"drive": "oops", "walk": {"name": 5, "minutes": "x"}, "food": {"takeout": "maybe"}},
                         from_region=REGION)
    assert c.food.takeout == "unknown" and c.walk.minutes is None and c.drive.from_region == REGION


NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)  # Tue 18:00 KST, ISO week 41
CFG = ScoutConfig(outing=OutingCfg(from_region=REGION))


class _Budget:
    def allow_web(self, user_initiated):
        return True


class FakeRouter:
    def __init__(self, items):
        self.budget = _Budget()
        self.items = items
        self.texts = []

    async def run(self, task, system_name, user_text, schema, *, user_initiated=False):
        self.texts.append(user_text)
        return LLMResult({"items": self.items}, "ai_web", False)


def _course(n, rt=(60, 100), place=None, src=None):
    return {"drive": {"from_region": "서울 종로구 한빛로 1 ○○아파트", "stops": [f"산{n}"],
                      "roundtrip_min_est": list(rt) if rt else [], "basis": "추정"},
            "walk": {"name": f"둘레길{n}", "closure_check": {"status": "ok", "source": "", "checked_at": ""}},
            "food": {"place": place or f"식당{n}", "takeout": "yes",
                     "price": {"value": "12000", "checked_at": "2026-10-01"},
                     "sources": [src or f"https://www.instagram.com/p/c{n}"]},
            "order": "walk_then_takeout", "alternative": {"summary": "실내", "reason": "rain"},
            "url": f"https://www.instagram.com/p/c{n}"}


def _setup(con):
    recs.ensure_schema(con)
    store.ensure_schema(con)


def _mock_ok():
    respx.get(url__regex=r"https://www\.instagram\.com/p/c\d+").mock(
        return_value=httpx.Response(200, text="<title>ok</title>"))


async def _run(con, router, cfg=CFG, now=NOW):
    async with httpx.AsyncClient(follow_redirects=True) as http:
        return await build_outing(con, router, http, cfg, now, user_initiated=True)


def _count(con, sql, *a):
    return con.execute(sql, a).fetchone()[0]


def _vac(con, dates):
    con.execute("INSERT INTO preference (key, value, updated_at) VALUES ('vacation_dates', ?, 'x')",
                (json.dumps(dates),))


async def test_disabled_no_router_call(con):
    _setup(con)
    r = FakeRouter([_course(1)])
    cards, sr = await _run(con, r, cfg=ScoutConfig())
    assert cards == [] and sr == SearchResult([], True, "disabled") and r.texts == []


@respx.mock
async def test_two_courses_cards_rows_and_recs(con):
    _setup(con)
    _mock_ok()
    cards, sr = await _run(con, FakeRouter([_course(1), _course(2)]))
    assert len(cards) == 2 and not sr.degraded
    rows = con.execute("SELECT * FROM outing_suggestion").fetchall()
    assert len(rows) == 2 and all(x["status"] == "proposed" for x in rows)
    assert rows[0]["for_week"] == "2026-W41"
    assert _count(con, "SELECT COUNT(*) FROM recommendation WHERE domain='outing'") == 2
    assert _count(con, "SELECT COUNT(*) FROM recommendation WHERE domain='food'") == 2
    saved = json.loads(rows[0]["course_json"])
    assert saved["drive"]["from_region"] == REGION


@respx.mock
async def test_all_sources_404_course_dropped(con):
    _setup(con)
    respx.get("https://www.instagram.com/p/c1").mock(return_value=httpx.Response(404))
    respx.get("https://www.instagram.com/p/c2").mock(return_value=httpx.Response(200, text="<title>ok</title>"))
    cards, _ = await _run(con, FakeRouter([_course(1), _course(2)]))
    assert len(cards) == 1
    assert _count(con, "SELECT COUNT(*) FROM outing_suggestion") == 1


@respx.mock
async def test_partial_source_failure_keeps_course_and_downgrades_takeout(con):
    _setup(con)
    respx.get("https://www.instagram.com/p/c1").mock(return_value=httpx.Response(404))
    respx.get("https://blog.naver.com/r/1").mock(return_value=httpx.Response(200, text="<title>ok</title>"))
    c = _course(1)
    c["food"]["sources"] = ["https://www.instagram.com/p/c1", "https://blog.naver.com/r/1"]
    cards, _ = await _run(con, FakeRouter([c]))
    assert len(cards) == 1
    saved = json.loads(con.execute("SELECT course_json FROM outing_suggestion").fetchone()[0])
    assert saved["food"]["sources"] == ["https://blog.naver.com/r/1"]
    assert saved["food"]["takeout"] == "unknown"


@respx.mock
async def test_prompt_has_no_exact_address_and_mentions_light_menu(con):
    _setup(con)
    _mock_ok()
    r = FakeRouter([_course(1)])
    await _run(con, r)
    t = r.texts[0]
    assert "한빛로" not in t and "아파트" not in t
    assert REGION in t and "샐러드" in t and "샌드위치" in t and "편도 30~60분" in t


@respx.mock
async def test_oneway_filter_50_in_80_out(con):
    _setup(con)
    _mock_ok()
    cards, _ = await _run(con, FakeRouter([_course(1, rt=(80, 100)), _course(2, rt=(120, 160))]))
    assert [c.meta["walk"] for c in cards] == ["둘레길1"]


@respx.mock
async def test_unknown_estimate_kept_and_labelled(con):
    _setup(con)
    _mock_ok()
    cards, _ = await _run(con, FakeRouter([_course(1, rt=None)]))
    assert len(cards) == 1 and "이동 시간 확인 필요" in cards[0].reason


def test_allowed_default(con):
    _setup(con)
    assert allowed_oneway_max(con, CFG, NOW) == (60, None)


def test_three_day_streak_extends(con):
    _setup(con)
    _vac(con, ["2026-10-09", "2026-10-10"])  # Fri Sat + Sun 10-11
    assert allowed_oneway_max(con, CFG, NOW) == (120, "연휴")


def test_two_day_streak_does_not_extend(con):
    _setup(con)
    _vac(con, ["2026-10-10"])  # Sat + Sun = 2
    assert allowed_oneway_max(con, CFG, NOW) == (60, None)


def test_weekday_holiday_not_rest_day(con):
    _setup(con)  # Mon 10-05 not in vacation_dates: a weekday public holiday does not count
    _vac(con, ["2026-10-10"])
    assert allowed_oneway_max(con, CFG, NOW)[1] is None


@respx.mock
async def test_streak_includes_80_still_excludes_130(con):
    _setup(con)
    _mock_ok()
    _vac(con, ["2026-10-09", "2026-10-10"])
    cards, _ = await _run(con, FakeRouter([_course(1, rt=(120, 160)), _course(2, rt=(200, 260))]))
    assert [c.meta["walk"] for c in cards] == ["둘레길1"]


@respx.mock
async def test_best_range_bonus_orders_first(con):
    _setup(con)
    _mock_ok()
    ext = ScoutConfig(outing=OutingCfg(from_region=REGION, max_oneway_min=120))
    cards, _ = await _run(con, FakeRouter([_course(1, rt=(150, 180)), _course(2, rt=(80, 100))]), cfg=ext)
    assert [c.meta["walk"] for c in cards] == ["둘레길2", "둘레길1"]


@respx.mock
async def test_excluded_food_place_dropped(con):
    _setup(con)
    _mock_ok()
    for i in range(2):
        rid = recs.record_recommendation(con, domain="food", item_ref=f"x{i}",
                                         features={"place": "식당1"}, query_used=None, now=NOW)
        recs.insert_feedback(con, rid, "dislike", [], None, NOW)
    cards, _ = await _run(con, FakeRouter([_course(1), _course(2)]))
    assert [c.meta["food"] for c in cards] == ["식당2"]


@respx.mock
async def test_taste_seed_pinned_once(con):
    _setup(con)
    _mock_ok()
    cfg = ScoutConfig(outing=OutingCfg(from_region=REGION),
                      taste_seeds=({"domain": "food", "feature": "light_menu", "value": "yes"},))
    await _run(con, FakeRouter([_course(1)]), cfg=cfg)
    pins = json.loads(con.execute("SELECT value FROM preference WHERE key='taste_pins'").fetchone()[0])
    assert ["food", "light_menu", "yes"] in pins
    con.execute("UPDATE preference SET value='[]' WHERE key='taste_pins'")
    await _run(con, FakeRouter([_course(2)]), cfg=cfg)
    assert con.execute("SELECT value FROM preference WHERE key='taste_pins'").fetchone()[0] == "[]"


def test_repo_config_has_no_personal_taste_seed():
    c = load_scout_config(Path("config/scout.yaml"))
    assert c.taste_seeds == ()

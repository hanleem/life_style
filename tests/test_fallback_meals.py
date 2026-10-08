from datetime import date, timedelta

import pytest
import yaml

from pa.core import fallback_meals, growth, household, meals

FAMILY = [{"role": "adult", "age": 40, "sex": "f"}, {"role": "adult", "age": 42, "sex": "m"},
          {"role": "child", "age": 10, "sex": "m"}, {"role": "child", "age": 8, "sex": "f"}]
WEEK = date(2026, 10, 10)                           # 토요일 시작


def _plan(start=WEEK, **kw):
    w = meals.week_window(start - timedelta(days=2))      # 목요일에 만든다
    return fallback_meals.plan_for(w, FAMILY, **kw), w


def test_yaml_has_four_weeks_with_every_slot_and_complete_dishes():
    data = yaml.safe_load(fallback_meals.PATH.read_text(encoding="utf-8"))
    assert len(data["weeks"]) == 4
    for w in data["weeks"]:
        assert [len(w[k]) for k in ("dinners", "breakfasts", "snacks", "lunches", "prep")] == [7, 7, 5, 2, 1]
        for k in ("dinners", "breakfasts", "snacks", "lunches", "prep"):
            for d in w[k]:
                assert d["title"] and d["time_min"] > 0 and d["steps"]
                assert d["ingredients"] and all(i["n"] and i["q"] and i["c"] for i in d["ingredients"])
        for d in w["dinners"] + w["lunches"]:
            assert len(d["sides"]) >= 2, d["title"]


def test_plan_follows_the_week_window_slots():
    plan, w = _plan()
    by = {}
    for m in plan["meals"]:
        by.setdefault(m["slot"], []).append(m["date"])
    days = [d.isoformat() for d in w["days"]]
    assert by["breakfast"] == days and by["dinner"] == days
    assert by["snack"] == [d.isoformat() for d in w["school_days"]] and len(by["snack"]) == 5
    assert by["prep"] == [w["prep_day"].isoformat()] and "lunch" not in by


def test_weekend_lunch_is_optional():
    plan, w = _plan(weekend_lunch=True)
    lunches = [m["date"] for m in plan["meals"] if m["slot"] == "lunch"]
    assert lunches == [d.isoformat() for d in w["weekend_days"]]


def test_plan_is_in_app_format_and_shows_veg_sides_and_spice():
    plan, _ = _plan()
    assert meals._clean(plan) is not None
    for m in plan["meals"]:
        assert m["title"] and m["servings"] == pytest.approx(household.total_portions(FAMILY)) and m["kid_spice"] in (0, 1)
        assert {"name", "qty"} <= m["ingredients"][0].keys()
    for m in plan["meals"]:
        if m["slot"] == "dinner":
            assert len(m["veg_sides"]) >= 2
    assert plan["shopping"] and all(g["category"] and g["items"] for g in plan["shopping"])
    assert plan["source"] == "rule"


@pytest.mark.parametrize("k", range(8))
def test_every_week_passes_the_growth_check(k):
    start = date.fromordinal(WEEK.toordinal() + 7 * k)
    plan, _ = _plan(start)
    r = growth.evaluate(plan)
    assert r["ok"], [(c["key"], c["got"], c["need"]) for c in r["checks"] if not c["ok"]]


def test_weekend_lunch_weeks_also_pass():
    for k in range(4):
        start = date.fromordinal(WEEK.toordinal() + 7 * k)
        plan, _ = _plan(start, weekend_lunch=True)
        assert growth.evaluate(plan)["ok"], k


def test_same_week_same_menu_and_weeks_rotate():
    a, _ = _plan()
    assert a == _plan()[0]
    titles = {tuple(m["title"] for m in _plan(date.fromordinal(WEEK.toordinal() + 7 * k))[0]["meals"]) for k in range(4)}
    assert len(titles) == 4


def test_allergies_and_dislikes_are_avoided():
    plan, _ = _plan(avoid=["달걀", "두부"])
    for m in plan["meals"]:
        text = m["title"] + " ".join(i["name"] for i in m["ingredients"])
        assert "달걀" not in text and "두부" not in text
    assert len(plan["meals"]) >= 19


def test_pantry_items_are_left_out_of_shopping_and_duplicates_merged():
    plan, _ = _plan(pantry=["양파", "우유"])
    names = [i["name"] for g in plan["shopping"] for i in g["items"]]
    assert "양파" not in names and "우유" not in names
    assert len(names) == len(set(names))


def test_quantities_scale_with_household_size():
    small = [FAMILY[0], FAMILY[2]]                         # 1.75인분
    big, w = _plan()
    plan_small = fallback_meals.plan_for(w, small)
    q = lambda p: next(i["qty"] for m in p["meals"] if m["slot"] == "dinner" for i in m["ingredients"] if i["qty"].endswith("g"))
    assert q(plan_small) != q(big)


def test_scale_qty_helper():
    s = fallback_meals.scale_qty
    assert s("600g", 0.5) == "300g" and s("1모", 2) == "2모" and s("1/2개", 2) == "1개"
    assert s("적당량", 3) == "적당량" and s("2개", 1.0) == "2개" and s("3개", 0.5) == "1.5개"

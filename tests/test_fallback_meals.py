from datetime import date

import yaml

from pa.core import fallback_meals, meals

DATES = [date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)]


def test_yaml_has_four_weeks_of_seven_complete_dinners():
    data = yaml.safe_load(fallback_meals.PATH.read_text(encoding="utf-8"))
    assert len(data["weeks"]) == 4
    for w in data["weeks"]:
        assert len(w["dinners"]) == 7
        for d in w["dinners"]:
            assert d["title"] and d["time_min"] > 0 and d["steps"]
            assert d["ingredients"] and all(i["n"] and i["q"] and i["c"] for i in d["ingredients"])


def test_plan_assigns_one_dinner_per_date_in_app_format():
    plan = fallback_meals.plan_for(DATES, date(2026, 10, 10), servings=4)
    assert [m["date"] for m in plan["meals"]] == [d.isoformat() for d in DATES]
    for m in plan["meals"]:
        assert m["title"] and m["slot"] == "cook" and m["servings"] == 4
        assert {"name", "qty"} <= m["ingredients"][0].keys()
    assert meals._clean(plan) is not None
    assert plan["shopping"] and all(g["category"] and g["items"] for g in plan["shopping"])


def test_same_week_gives_same_menu_and_weeks_rotate():
    a = fallback_meals.plan_for(DATES, date(2026, 10, 10))
    assert a == fallback_meals.plan_for(DATES, date(2026, 10, 10))
    titles = {tuple(m["title"] for m in fallback_meals.plan_for(DATES, date(2026, 10, 10) + (date(2026, 10, 17) - date(2026, 10, 10)) * k)["meals"])
              for k in range(4)}
    assert len(titles) == 4


def test_pantry_items_are_left_out_of_shopping_and_duplicates_merged():
    plan = fallback_meals.plan_for(DATES, date(2026, 10, 10), pantry=["양파", "두부"])
    names = [i["name"] for g in plan["shopping"] for i in g["items"]]
    assert "양파" not in names and "두부" not in names
    assert len(names) == len(set(names))


def test_more_dates_than_dinners_wraps_around():
    many = [date(2026, 10, 10 + i) for i in range(9)]
    plan = fallback_meals.plan_for(many, date(2026, 10, 10))
    assert len(plan["meals"]) == 9

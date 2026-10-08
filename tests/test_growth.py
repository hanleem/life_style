import pytest

from pa.core import growth


def meal(slot, title, ings=(), sides=(), date="2026-10-10"):
    return {"date": date, "slot": slot, "title": title, "veg_sides": list(sides),
            "ingredients": [{"name": n, "qty": "1"} for n in ings]}


def test_food_group_detection_from_ingredients_and_title():
    assert growth.groups(meal("dinner", "두부조림", ["두부"])) == {"calcium"}
    assert growth.groups(meal("dinner", "소불고기", ["불고기용 소고기"])) == {"iron"}
    assert growth.groups(meal("breakfast", "계란말이", ["달걀"])) == {"iron"}
    assert growth.groups(meal("breakfast", "계란 토스트", ["식빵", "계란", "치즈"])) == {"iron", "calcium"}
    assert growth.groups(meal("dinner", "고등어구이", ["고등어"])) == {"fish"}
    assert growth.groups(meal("snack", "요거트와 과일", ["플레인 요구르트", "딸기"])) == {"calcium"}
    assert growth.groups(meal("snack", "멸치 주먹밥", ["잔멸치", "밥"])) == {"calcium"}
    assert growth.groups(meal("dinner", "시금치 된장국", ["시금치", "된장"])) == {"iron"}
    assert growth.groups(meal("dinner", "돈가스", ["돼지고기 등심"])) == {"junk"}
    assert growth.groups(meal("snack", "탄산음료", [])) == {"junk"}
    assert growth.groups(meal("dinner", "나물밥", ["콩나물"])) == set()


def test_plain_milk_and_cheese_count_as_calcium():
    assert "calcium" in growth.groups(meal("snack", "우유와 시리얼", ["우유"]))
    assert "calcium" in growth.groups(meal("snack", "치즈", ["슬라이스 치즈"]))


def _good_week():
    ms = []
    for d in range(7):
        ms.append(meal("breakfast", "계란밥과 우유", ["달걀", "우유"], date=f"d{d}"))
        ms.append(meal("dinner", f"저녁{d}", ["시금치"] if d < 4 else ["돼지고기"], ["시금치나물", "콩나물무침"], date=f"d{d}"))
    ms[1]["ingredients"].append({"name": "고등어", "qty": "1"})
    ms[3]["ingredients"].append({"name": "연어", "qty": "1"})
    return {"meals": ms}


def test_a_good_week_passes_every_check():
    r = growth.evaluate(_good_week())
    assert r["ok"] and {c["key"] for c in r["checks"]} == {"calcium", "iron", "fish", "veg_sides", "junk"}
    assert all(c["ok"] for c in r["checks"]) and r["failed"] == []


def test_thresholds_are_the_agreed_ones():
    t = growth.THRESHOLDS
    assert t == {"calcium": 7, "iron": 4, "fish": 2, "veg_sides": 2, "junk": 2}


def test_calcium_needs_seven_meals_in_the_week():
    plan = _good_week()
    breakfasts = [m for m in plan["meals"] if m["slot"] == "breakfast"]
    for m in breakfasts[:3]:                          # 우유 든 아침 7끼 중 3끼에서 우유를 뺀다 → 4끼
        m["title"], m["ingredients"] = "계란밥", [{"name": "달걀", "qty": "1"}]
    r = growth.evaluate(plan)
    cal = next(c for c in r["checks"] if c["key"] == "calcium")
    assert cal["got"] == 4 and not cal["ok"] and "calcium" in r["failed"] and not r["ok"]


def test_boundary_values_pass_and_one_less_fails():
    base = {"meals": [meal("snack", f"s{i}", ["우유"]) for i in range(7)]}
    assert next(c for c in growth.evaluate(base)["checks"] if c["key"] == "calcium")["ok"]
    base["meals"].pop()
    assert not next(c for c in growth.evaluate(base)["checks"] if c["key"] == "calcium")["ok"]


def test_junk_is_a_maximum_not_a_minimum():
    ms = [meal("snack", f"치킨{i}", ["닭"]) for i in range(2)]
    assert next(c for c in growth.evaluate({"meals": ms})["checks"] if c["key"] == "junk")["ok"]
    ms.append(meal("snack", "감자튀김", ["감자"]))
    j = next(c for c in growth.evaluate({"meals": ms})["checks"] if c["key"] == "junk")
    assert j["got"] == 3 and not j["ok"] and j["kind"] == "max"


def test_veg_sides_checks_every_dinner_and_weekend_lunch_but_not_breakfast_or_snack():
    plan = {"meals": [meal("dinner", "a", [], ["나물", "무침"]), meal("dinner", "b", [], ["나물"]),
                      meal("breakfast", "c", [], []), meal("snack", "d", [], []), meal("lunch", "e", [], [])]}
    c = next(x for x in growth.evaluate(plan)["checks"] if x["key"] == "veg_sides")
    assert c["need"] == 3 and c["got"] == 1 and not c["ok"]          # 저녁 2끼 + 주말 점심 1끼 중 1끼만 충족


def test_prep_dishes_are_not_counted_as_meals():
    plan = {"meals": [meal("prep", "멸치볶음", ["잔멸치"]), meal("prep", "두부조림", ["두부"])]}
    assert next(c for c in growth.evaluate(plan)["checks"] if c["key"] == "calcium")["got"] == 0


def test_empty_or_broken_plan_is_safe():
    assert growth.evaluate({})["ok"] is False
    assert growth.evaluate({"meals": [None, "x", {"title": None, "ingredients": "bad"}]})["checks"]
    assert growth.evaluate(None)["ok"] is False


def test_followup_lines_name_only_failed_checks():
    r = growth.evaluate({"meals": [meal("dinner", "a", ["달걀"], ["나물", "무침"])]})
    lines = "\n".join(growth.followup_lines(r))
    assert "칼슘" in lines and "생선" in lines and "철분" in lines
    assert "튀김" not in lines
    assert growth.followup_lines(growth.evaluate(_good_week())) == []


def test_badge_text():
    good = growth.evaluate(_good_week())
    assert growth.badge(good) == ("ok", "이번 주 성장 영양 체크 · 모두 충족")
    bad = growth.evaluate({"meals": [meal("dinner", "a", ["달걀"])]})
    kind, text = growth.badge(bad)
    assert kind == "warn" and "부족" in text and "이번 주 성장 영양 체크" in text


@pytest.mark.parametrize("name", ["두부", "순두부", "연두부"])
def test_tofu_variants(name):
    assert "calcium" in growth.groups(meal("dinner", "x", [name]))

"""냉동 보관할 수 있는 재료는 대략 몇 인분인지로 세고, 식단에서 쓸 때마다 그 끼니의 인분만큼 뺀다(약 2인분 남으면 주문 판단)."""
from datetime import date
from pathlib import Path

import pytest

from pa.core import pantry

D = date(2026, 10, 8)


def test_freezable_bulk_is_counted_in_servings_and_each_use_deducts_the_meal_servings(con):
    pantry.record_cart(con, "돼지고기", "한돈 불고기용 1.5kg", "https://www.coupang.com/vp/products/1", "1.5kg", None, 15, "인분")
    pantry.add_item(con, "돼지고기(불고기용)", bought_on=D)                      # '샀어요' → 15인분 재고
    s = next(x for x in pantry.list_staples(con) if x["name"] == "돼지고기")
    assert (s["count"], s["unit"], s["reorder_at"]) == (15, "인분", pantry.SERVING_REORDER)
    plan = {"week_start": "2026-10-10", "meals": [
        {"date": "2026-10-10", "slot": "cook", "servings": 4, "ingredients": [{"name": "돼지고기(불고기용 슬라이스)"}]},
        {"date": "2026-10-11", "slot": "cook", "servings": 1, "ingredients": [{"name": "새우"}]},
        {"date": "2026-10-12", "slot": "lunchbox", "ingredients": [{"name": "돼지고기"}]}]}                # servings 없으면 1인분
    assert pantry.consume_staples(con, plan, date(2026, 10, 13)) == 2
    assert next(x for x in pantry.list_staples(con) if x["name"] == "돼지고기")["count"] == 10     # 15 - 4 - 1
    pantry.set_staple(con, "돼지고기", 2, 2, "돼지고기", "인분")
    assert [x["name"] for x in pantry.low_staples(con)] == ["돼지고기"]                              # 2인분 남으면 주문 판단
    block = "\n".join(pantry.prompt_block(con, D))
    assert "냉동해 둔 재고" in block and "돼지고기 2인분" in block


def test_servings_counting_is_only_for_freezable_ingredients(con):
    with pytest.raises(ValueError):
        pantry.record_cart(con, "오이", "백오이", "https://www.coupang.com/vp/products/2", "", None, 10, "인분")
    assert pantry.is_freezable("대파(냉동)") and pantry.is_freezable("닭다리살") and not pantry.is_freezable("가지")
    p = Path(__file__).resolve().parent.parent / "config" / "prompts" / "meal_plan.md"
    txt = p.read_text(encoding="utf-8")
    assert "냉동해 둔 재고" in txt and "약 2인분 남았을 때" in txt


def test_piece_counted_staples_still_deduct_one_per_use(con):
    pantry.set_staple(con, "즉석밥", 10, 4, "즉석밥,밥")
    plan = {"week_start": "2026-10-10", "meals": [{"date": "2026-10-10", "slot": "cook", "servings": 4, "ingredients": [{"name": "즉석밥"}]}]}
    pantry.consume_staples(con, plan, date(2026, 10, 11))
    assert next(x for x in pantry.list_staples(con) if x["name"] == "즉석밥")["count"] == 9

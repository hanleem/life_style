"""칼로리 기준 식단: 한 끼 목표 칼로리로 '1인분 몇 g'을 정하고, 단백질·탄수화물·지방이 많은 재료만 그렇게 인분 환산한다."""
from datetime import date
from pathlib import Path

from pa.core import meals, nutrition, pantry

D = date(2026, 10, 8)


def test_only_macro_rich_foods_have_calorie_info():
    assert nutrition.lookup("돼지고기(불고기용 슬라이스)") and nutrition.lookup("두부(단단한 두부)") and nutrition.lookup("즉석밥")
    assert nutrition.lookup("올리브오일") and nutrition.lookup("우동면(또는 중화면)")
    assert nutrition.lookup("가지") is None and nutrition.lookup("대파") is None and nutrition.lookup("미림") is None   # 채소·양념은 제외


def test_serving_is_the_amount_that_fills_the_ingredients_share_of_a_meal():
    # 600kcal 한 끼에서 단백질 재료 35% = 210kcal → 돼지고기(100g당 170kcal)는 약 124g
    assert nutrition.serving_grams("돼지고기") == 123.5
    assert nutrition.serving_grams("닭가슴살") == 190.9                     # 더 가벼운 재료는 더 많이 먹어야 같은 몫이 된다
    assert nutrition.servings_for("돼지고기", 300) == 2.4
    assert nutrition.serving_grams("가지") is None and nutrition.servings_for("가지", 300) is None
    assert nutrition.serving_grams("돼지고기", 500) < nutrition.serving_grams("돼지고기", 700)   # 목표 칼로리를 바꾸면 인분이 달라진다


def test_quantities_are_read_as_grams_without_inventing_anything():
    assert nutrition.parse_grams("돼지고기", "150g") == 150 and nutrition.parse_grams("연어", "1.5kg") == 1500
    assert nutrition.parse_grams("계란", "2개") == 100 and nutrition.parse_grams("두부", "1모") == 300 and nutrition.parse_grams("밥", "1공기") == 130
    assert nutrition.parse_grams("돼지고기", "적당량") is None and nutrition.parse_grams("가지", "1개") is None


def test_meal_kcal_counts_only_readable_macro_rich_ingredients():
    meal = {"ingredients": [{"name": "돼지고기", "qty": "120g"}, {"name": "밥", "qty": "1공기"}, {"name": "가지", "qty": "1개"},
                            {"name": "참기름", "qty": "적당량"}]}
    assert nutrition.meal_kcal(meal) == round(170 * 1.2 + 150 * 1.3)           # 204 + 195 (채소·양을 모르는 재료는 뺀다)
    assert nutrition.meal_kcal({"ingredients": [{"name": "가지", "qty": "2개"}]}) is None


def test_rules_read_the_owner_target_from_config():
    kcal, shares = nutrition.rules({"prefs": {"meal_kcal": 500, "calorie_shares": {"protein": 0.4}}})
    assert kcal == 500 and shares["protein"] == 0.4 and shares["carb"] == 0.45
    assert nutrition.rules(None)[0] == nutrition.DEFAULT_MEAL_KCAL
    assert nutrition.rules({"prefs": {"meal_kcal": "abc"}})[0] == nutrition.DEFAULT_MEAL_KCAL
    cfg = meals.load_config()
    assert cfg["prefs"]["meal_kcal"] is None                                      # 기본 설정은 값을 비워 두고 기본값(600)을 쓴다
    assert nutrition.rules(cfg)[0] == nutrition.DEFAULT_MEAL_KCAL


def test_cart_servings_come_from_grams_and_use_deducts_calorie_based_servings(con):
    pantry.record_cart(con, "돼지고기", "한돈 불고기용 300g", "https://www.coupang.com/vp/products/1", "300g", None, None, "인분")
    assert con.execute("SELECT count FROM cart_item").fetchone()[0] == 2.4      # 300g ≈ 2.4인분(한 끼 600kcal 기준)
    pantry.add_item(con, "돼지고기(불고기용)", bought_on=D)
    plan = {"week_start": "2026-10-10", "meals": [
        {"date": "2026-10-10", "slot": "cook", "servings": 3, "ingredients": [{"name": "돼지고기(불고기용 슬라이스)", "qty": "120g"}]}]}
    pantry.consume_staples(con, plan, date(2026, 10, 11))
    left = next(s for s in pantry.list_staples(con) if s["name"] == "돼지고기")["count"]
    assert abs(left - (2.4 - 1.0)) < 0.11                                      # 120g ≈ 1.0인분을 뺀다(끼니 인분 3이 아니라 칼로리 환산)


def test_prompt_carries_target_calories_and_the_meal_plan_rule(con):
    meals.ensure_schema(con)
    txt = meals._prompt(con, meals.week_window(D), meals.load_config(), "일식")
    assert "[칼로리" in txt and "끼니당 목표 칼로리: 약 600kcal" in txt and "100g당 대략 열량" in txt
    rule = (Path(__file__).resolve().parent.parent / "config" / "prompts" / "meal_plan.md").read_text(encoding="utf-8")
    assert "g 단위로" in rule and "다이어트" not in rule

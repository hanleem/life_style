import pytest

from pa.core import household

FAMILY = [{"role": "adult", "age": 40, "sex": "f"}, {"role": "adult", "age": 42, "sex": "m"},
          {"role": "child", "age": 10, "sex": "m"}, {"role": "child", "age": 8, "sex": "f"}]


@pytest.mark.parametrize("age,expect", [(6, 0.6), (8, 0.6), (9, 0.75), (11, 0.75), (12, 0.9), (15, 0.9), (18, 0.9)])
def test_child_portion_by_age(age, expect):
    assert household.portion({"role": "child", "age": age, "sex": "m"}) == expect


def test_adult_is_one_portion_whatever_the_age():
    assert household.portion({"role": "adult", "age": 70, "sex": "f"}) == 1.0


def test_family_total_is_the_sum_of_portions():
    assert household.total_portions(FAMILY) == pytest.approx(2 + 0.75 + 0.6)


def test_small_children_and_empty_household():
    assert household.portion({"role": "child", "age": 3, "sex": "f"}) == 0.6
    assert household.total_portions([]) == 0


@pytest.mark.parametrize("m,kcal", [
    ({"role": "child", "age": 6, "sex": "m"}, 1700), ({"role": "child", "age": 8, "sex": "m"}, 1700),
    ({"role": "child", "age": 7, "sex": "f"}, 1500), ({"role": "child", "age": 8, "sex": "f"}, 1500),
    ({"role": "child", "age": 9, "sex": "m"}, 1900), ({"role": "child", "age": 11, "sex": "m"}, 1900),
    ({"role": "child", "age": 10, "sex": "f"}, 1700), ({"role": "adult", "age": 40, "sex": "f"}, 1900),
    ({"role": "adult", "age": 42, "sex": "m"}, 2500)])
def test_daily_kcal_table(m, kcal):
    assert household.daily_kcal(m) == kcal


def test_kcal_can_be_overridden_per_person():
    assert household.daily_kcal({"role": "adult", "age": 40, "sex": "f", "kcal": 1600}) == 1600


def test_older_and_younger_children_use_nearest_row():
    assert household.daily_kcal({"role": "child", "age": 4, "sex": "m"}) == 1700     # 표의 가장 어린 구간
    assert household.daily_kcal({"role": "child", "age": 13, "sex": "f"}) == 1700    # 표의 가장 큰 아이 구간


def test_meal_target_uses_slot_shares():
    assert household.meal_kcal(FAMILY[0], "dinner") == round(1900 * household.SLOT_SHARE["dinner"])
    assert sum(household.SLOT_SHARE[s] for s in ("breakfast", "dinner", "snack")) < 1       # 점심은 학교·밖에서


def test_prompt_lines_describe_household_without_names():
    lines = "\n".join(household.prompt_lines(FAMILY))
    assert "3.35인분" in lines and "어른 2명" in lines and "아이 2명" in lines
    assert "10세" in lines and "8세" in lines and "1,500" in lines
    assert "첫째" not in lines
    lines2 = "\n".join(household.prompt_lines([{**FAMILY[2], "name": "첫째"}]))
    assert "첫째" not in lines2                       # 이름은 AI 에 보내지 않는다


def test_describe_for_screen():
    assert household.summary(FAMILY) == "어른 2명 · 아이 2명 (총 3.35인분)"

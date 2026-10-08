import pytest

from pa.core import profile


def _answers(**over):
    base = {"honorific": "하늘님", "tone": "friendly",
            "members": [{"role": "adult", "name": "", "age": 40, "sex": "f"},
                        {"role": "adult", "name": "", "age": 42, "sex": "m"},
                        {"role": "child", "name": "첫째", "age": 10, "sex": "m"},
                        {"role": "child", "name": "둘째", "age": 8, "sex": "f"}],
            "allergies": ["땅콩"], "dislikes": ["가지"], "cook_skill": "intermediate", "weekday_cook_min": 30,
            "weekly_budget_won": 200000, "shops": ["coupang"], "interests": ["kids_edu", "outing"],
            "theme": "pastel", "font_size": "large"}
    base.update(over)
    return base


def test_valid_answers_roundtrip(tmp_path):
    assert not profile.exists(tmp_path)
    p = profile.save(tmp_path, _answers())
    assert profile.exists(tmp_path)
    assert profile.load(tmp_path) == p
    assert p["theme"] == "pastel" and p["tone"] == "friendly" and len(p["members"]) == 4


def test_missing_file_means_no_profile(tmp_path):
    assert profile.load(tmp_path) is None
    assert profile.load(tmp_path / "nope") is None


def test_broken_file_means_no_profile(tmp_path):
    (tmp_path / profile.FILENAME).write_text("{{{ not yaml", encoding="utf-8")
    assert profile.load(tmp_path) is None
    (tmp_path / profile.FILENAME).write_text("- a\n- b\n", encoding="utf-8")
    assert profile.load(tmp_path) is None


@pytest.mark.parametrize("bad", [
    {"tone": "rude"}, {"theme": "neon"}, {"font_size": "huge"}, {"cook_skill": "pro"}, {"members": []},
    {"members": [{"role": "robot", "age": 3, "sex": "f"}]},
    {"members": [{"role": "child", "age": 99, "sex": "f"}]},
    {"members": [{"role": "adult", "age": 30, "sex": "x"}]},
    {"weekday_cook_min": 0}, {"weekly_budget_won": -1}, {"shops": []}, {"shops": ["moon"]},
    {"interests": ["gambling"]}, {"honorific": "x" * 40},
])
def test_invalid_answers_are_rejected(bad):
    with pytest.raises(profile.ProfileError):
        profile.normalize(_answers(**bad))


def test_text_lists_are_split_trimmed_and_capped():
    p = profile.normalize(_answers(allergies="땅콩, 우유\n 새우 ,, ", dislikes="가지" + ",가지" * 40))
    assert p["allergies"] == ["땅콩", "우유", "새우"]
    assert len(p["dislikes"]) <= profile.MAX_LIST


def test_blank_member_rows_are_ignored_and_name_is_optional():
    p = profile.normalize(_answers(members=[{"role": "adult", "age": 38, "sex": "f"},
                                            {"role": "", "age": "", "sex": ""}]))
    assert len(p["members"]) == 1 and p["members"][0]["name"] == ""


def test_numbers_from_forms_arrive_as_strings():
    p = profile.normalize(_answers(weekday_cook_min="45", weekly_budget_won="150000",
                                   members=[{"role": "child", "age": "9", "sex": "m"}]))
    assert p["weekday_cook_min"] == 45 and p["weekly_budget_won"] == 150000 and p["members"][0]["age"] == 9


def test_unknown_keys_are_dropped():
    assert "evil" not in profile.normalize(_answers(evil="x"))


def test_defaults_are_a_valid_profile():
    p = profile.normalize(profile.defaults())
    assert p["theme"] == "fresh" and p["font_size"] == "normal" and p["members"]


def test_save_is_atomic_and_overwrites(tmp_path):
    profile.save(tmp_path, _answers())
    profile.save(tmp_path, _answers(theme="wood"))
    assert profile.load(tmp_path)["theme"] == "wood"
    assert [f.name for f in tmp_path.iterdir()] == [profile.FILENAME]


def test_theme_and_font_helpers():
    assert profile.theme_attrs(None) == {"theme": "fresh", "font": "normal"}
    assert profile.theme_attrs(_answers()) == {"theme": "pastel", "font": "large"}
    assert profile.theme_attrs({"theme": "evil\"><script>"}) == {"theme": "fresh", "font": "normal"}

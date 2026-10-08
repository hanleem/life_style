"""첫 인터뷰(/welcome): 프로필이 없으면 모든 화면이 여기로 오고, 답하면 앱이 바로 바뀐다."""
import pytest
from conftest import owner_client
from fastapi.testclient import TestClient

from pa.core import profile
from pa.settings import Settings
from pa.web import routes_welcome
from pa.web.app import create_app


class FakeRouter:
    tone = "polite"
    honorific = ""


@pytest.fixture
def env(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path, require_profile=True)
    router = FakeRouter()
    app = create_app(s, con, router)
    return app, owner_client(app), tmp_path, router


STEP_ANSWERS = {
    "honorific": {"honorific": "하늘님"},
    "tone": {"tone": "friendly"},
    "family": {"role_0": "adult", "age_0": "40", "sex_0": "f", "name_0": "",
               "role_1": "adult", "age_1": "42", "sex_1": "m", "name_1": "",
               "role_2": "child", "age_2": "10", "sex_2": "m", "name_2": "첫째",
               "role_3": "child", "age_3": "8", "sex_3": "f", "name_3": "둘째"},
    "food": {"allergies": "땅콩, 새우", "dislikes": "가지"},
    "cook": {"cook_skill": "beginner", "weekday_cook_min": "30"},
    "budget": {"budget_preset": "200000", "budget_custom": ""},
    "shops": {"shops": ["coupang", "mart"]},
    "interests": {"interests": ["kids_edu", "outing"]},
    "look": {"theme": "wood", "font_size": "large"},
}


def _run_all(c):
    for key in routes_welcome.STEP_KEYS:
        r = c.post(f"/welcome/{key}", data=STEP_ANSWERS[key], follow_redirects=False)
        assert r.status_code == 303, (key, r.text[:300])


def test_every_page_goes_to_welcome_until_profile_exists(env):
    _, c, _, _ = env
    for path in ("/", "/tasks", "/meals", "/settings", "/approvals", "/routine", "/outing"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/welcome", path
    assert c.get("/health").status_code == 200
    assert c.get("/static/tokens.css").status_code == 200
    assert c.get("/welcome", follow_redirects=False).status_code in (200, 303)


def test_api_writes_are_refused_without_profile(env):
    _, c, _, _ = env
    r = c.post("/api/tasks", json={"title": "x"})
    assert r.status_code == 409 and "첫 설정" in r.json()["detail"]


def test_stranger_cannot_use_the_interview(env):
    app, _, _, _ = env
    stranger = TestClient(app, base_url="http://localhost:8010", client=("203.0.113.9", 5000))
    assert stranger.get("/welcome", follow_redirects=False).status_code in (401, 403)
    assert stranger.post("/welcome/honorific", data={"honorific": "x"}, follow_redirects=False).status_code in (401, 403)


def test_welcome_starts_at_first_question_one_per_screen(env):
    _, c, _, _ = env
    r = c.get("/welcome", follow_redirects=True)
    assert r.status_code == 200 and "1 / 9" in r.text and 'name="honorific"' in r.text
    assert 'name="tone"' not in r.text                       # 한 화면에 질문 하나


def test_full_interview_saves_profile_and_applies_it(env):
    _, c, data_dir, router = env
    c.get("/welcome", follow_redirects=True)
    _run_all(c)
    p = profile.load(data_dir)
    assert p["honorific"] == "하늘님" and p["tone"] == "friendly" and len(p["members"]) == 4
    assert p["allergies"] == ["땅콩", "새우"] and p["weekly_budget_won"] == 200000
    assert p["shops"] == ["coupang", "mart"] and p["interests"] == ["kids_edu", "outing"]
    assert p["theme"] == "wood" and p["font_size"] == "large"
    assert router.tone == "friendly" and router.honorific == "하늘님"        # AI 말투에 바로 반영
    home = c.get("/")
    assert home.status_code == 200
    assert 'data-theme="wood"' in home.text and 'data-font="large"' in home.text
    assert "하늘님" in home.text


def test_custom_budget_wins_over_preset(env):
    _, c, data_dir, _ = env
    c.get("/welcome", follow_redirects=True)
    answers = {**STEP_ANSWERS, "budget": {"budget_preset": "100000", "budget_custom": "175,000"}}
    for key in routes_welcome.STEP_KEYS:
        c.post(f"/welcome/{key}", data=answers[key], follow_redirects=False)
    assert profile.load(data_dir)["weekly_budget_won"] == 175000


def test_bad_answer_stays_on_the_step_with_an_easy_message(env):
    _, c, data_dir, _ = env
    c.get("/welcome", follow_redirects=True)
    r = c.post("/welcome/cook", data={"cook_skill": "wizard", "weekday_cook_min": "30"}, follow_redirects=False)
    assert r.status_code == 400 and "요리 실력" in r.text and 'name="cook_skill"' in r.text
    assert not profile.exists(data_dir)


def test_back_keeps_earlier_answers(env):
    _, c, _, _ = env
    c.get("/welcome", follow_redirects=True)
    c.post("/welcome/honorific", data={"honorific": "하늘님"}, follow_redirects=False)
    c.post("/welcome/tone", data={"tone": "friendly"}, follow_redirects=False)
    back = c.get("/welcome/honorific")
    assert 'value="하늘님"' in back.text
    again = c.get("/welcome/tone")
    assert 'value="friendly" checked' in again.text.replace("  ", " ") or "checked" in again.text
    assert '/welcome/honorific"' in again.text               # 뒤로 가기 링크


def test_unknown_step_is_404(env):
    _, c, _, _ = env
    assert c.get("/welcome/nope").status_code == 404
    assert c.post("/welcome/nope", data={}).status_code == 404


def test_only_the_last_step_writes_the_profile(env):
    _, c, data_dir, _ = env
    c.get("/welcome", follow_redirects=True)
    for key in routes_welcome.STEP_KEYS[:-1]:
        c.post(f"/welcome/{key}", data=STEP_ANSWERS[key], follow_redirects=False)
        assert not profile.exists(data_dir)
    r = c.post("/welcome/look", data=STEP_ANSWERS["look"], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/welcome/done" and profile.exists(data_dir)
    assert "준비가 끝났어요" in c.get("/welcome/done").text


def test_redo_prefills_from_saved_profile(env):
    _, c, data_dir, _ = env
    profile.save(data_dir, {**profile.defaults(), "honorific": "바다님", "theme": "mono"})
    r = c.get("/welcome?restart=1", follow_redirects=True)
    assert 'value="바다님"' in r.text
    look = c.get("/welcome/look")
    assert "mono" in look.text and "checked" in look.text


def test_theme_step_previews_all_four_themes(env):
    _, c, _, _ = env
    c.get("/welcome", follow_redirects=True)
    html = c.get("/welcome/look").text
    for t in ("pastel", "wood", "mono", "fresh"):
        assert f'data-theme="{t}"' in html
    assert 'name="font_size"' in html


def test_settings_has_redo_link(env, tmp_path):
    _, c, data_dir, _ = env
    profile.save(data_dir, profile.defaults())
    html = c.get("/settings").text
    assert "/welcome?restart=1" in html and "인터뷰 다시 하기" in html


def test_page_without_profile_gate_flag_still_works(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path, require_profile=False)
    assert owner_client(create_app(s, con, FakeRouter())).get("/", follow_redirects=False).status_code == 200


def test_family_step_ignores_blank_rows_and_shows_extra_row_button(env):
    _, c, _, _ = env
    c.get("/welcome", follow_redirects=True)
    html = c.get("/welcome/family").text
    assert 'name="role_0"' in html and 'name="role_7"' in html and "한 명 더" in html


def test_cook_step_saves_weekend_lunch_and_family_step_saves_kcal(env):
    _, c, data_dir, _ = env
    c.get("/welcome", follow_redirects=True)
    answers = {**STEP_ANSWERS, "cook": {**STEP_ANSWERS["cook"], "weekend_lunch": "1"},
               "family": {**STEP_ANSWERS["family"], "kcal_0": "1700"}}
    for key in routes_welcome.STEP_KEYS:
        c.post(f"/welcome/{key}", data=answers[key], follow_redirects=False)
    p = profile.load(data_dir)
    assert p["weekend_lunch"] is True and p["members"][0]["kcal"] == 1700 and "kcal" not in p["members"][1]
    html = c.get("/welcome?restart=1", follow_redirects=True) and c.get("/welcome/family").text
    assert 'name="kcal_0"' in html and 'value="1700"' in html

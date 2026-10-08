"""아이 루틴 화면: 설정 저장, 내일 준비물, 홈 카드."""
from datetime import date

import pytest
from conftest import owner_client
from fastapi.testclient import TestClient

from pa.core import kids, profile
from pa.settings import Settings
from pa.web import routes_kids
from pa.web.app import create_app


class FakeRouter:
    tone = "polite"
    honorific = ""


MEMBERS = [{"role": "adult", "age": 40, "sex": "f"}, {"role": "child", "age": 10, "sex": "m", "name": "민준"},
           {"role": "child", "age": 8, "sex": "f"}]


@pytest.fixture
def env(con, tmp_path):
    profile.save(tmp_path, {**profile.defaults(), "members": MEMBERS, "interests": ["kids_edu"]})
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    app = create_app(s, con, FakeRouter())
    return app, owner_client(app, follow_redirects=False), con, tmp_path


FORM = {"prep_time": "20:00",
        "c0_days": ["mon", "tue", "wed", "thu", "fri"], "c0_start": "08:40", "c0_end": "13:30",
        "c0_k0_name": "영어학원", "c0_k0_days": ["tue", "thu"], "c0_k0_start": "16:00", "c0_k0_bring": "영어 책가방, 단어장",
        "c1_days": ["mon", "tue", "wed", "thu", "fri"], "c1_start": "09:00", "c1_end": "12:40"}


def test_kids_page_requires_owner(env):
    app, _, _, _ = env
    s = TestClient(app, base_url="http://localhost:8010", client=("203.0.113.9", 5000), follow_redirects=False)
    for path in ("/kids", "/kids/settings"):
        assert s.get(path).status_code == 401
    assert s.post("/kids/settings", data=FORM).status_code == 401


def test_settings_form_has_a_section_per_profile_child_with_their_names(env):
    _, c, _, _ = env
    html = c.get("/kids/settings").text
    assert "민준(10세)" in html and "둘째(8세)" in html
    assert 'name="c0_days"' in html and 'name="c1_days"' in html and 'name="c2_days"' not in html
    assert 'name="c0_k0_name"' in html and f'name="c0_k{kids.MAX_CLASSES - 1}_name"' in html and 'name="prep_time"' in html
    assert 'type="time"' in html


def test_settings_without_children_points_to_the_interview(con, tmp_path):
    profile.save(tmp_path, {**profile.defaults()})
    app = create_app(Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path), con, FakeRouter())
    html = owner_client(app).get("/kids/settings").text
    assert "아이가 없어요" in html and "/welcome?restart=1" in html


def test_saving_the_form_stores_the_schedule(env):
    _, c, con, _ = env
    r = c.post("/kids/settings", data=FORM)
    assert r.status_code == 303 and r.headers["location"] == "/kids"
    saved = kids.load(con)
    assert saved["children"][0]["classes"][0] == {"name": "영어학원", "days": ["tue", "thu"], "start": "16:00", "bring": ["영어 책가방", "단어장"]}
    assert saved["children"][0]["school_start"] == "08:40" and saved["children"][1]["school_end"] == "12:40"


def test_saved_values_come_back_in_the_form(env):
    _, c, _, _ = env
    c.post("/kids/settings", data=FORM)
    html = c.get("/kids/settings").text
    assert 'value="영어학원"' in html and 'value="08:40"' in html and 'value="영어 책가방, 단어장"' in html


def test_bad_time_gets_a_friendly_error_and_keeps_what_was_typed(env):
    _, c, con, _ = env
    r = c.post("/kids/settings", data={**FORM, "c0_start": "아침"})
    assert r.status_code == 400 and "등교 시각" in r.text and 'value="영어학원"' in r.text
    assert kids.load(con) == kids.defaults()


def test_class_without_days_is_refused(env):
    _, c, _, _ = env
    r = c.post("/kids/settings", data={**FORM, "c0_k0_days": []})
    assert r.status_code == 400 and "요일" in r.text


def test_kids_page_shows_tomorrows_items_by_child(env, monkeypatch):
    _, c, con, _ = env
    c.post("/kids/settings", data=FORM)
    monkeypatch.setattr(routes_kids, "_today", lambda: date(2026, 10, 12))        # 월요일 → 내일은 화요일(영어학원)
    html = c.get("/kids").text
    assert "내일(화) 준비물" in html and "민준(10세)" in html and "영어 책가방" in html and "단어장" in html
    assert "영어학원 16:00" in html and "오늘(월)" in html and "등교 08:40" in html


def test_kids_page_empty_states(env, monkeypatch):
    _, c, _, _ = env
    monkeypatch.setattr(routes_kids, "_today", lambda: date(2026, 10, 12))
    html = c.get("/kids").text
    assert "내일 챙길 준비물이 없어요" in html and "/kids/settings" in html and "/notice" in html


def test_pending_notices_are_linked_from_the_kids_page(env, monkeypatch):
    _, c, con, _ = env
    monkeypatch.setattr(routes_kids, "_today", lambda: date(2026, 10, 12))
    con.execute("INSERT INTO notice_event(id,created_at,title,status) VALUES('n','t','행사','new')")
    assert "확인할 가정통신문 1건" in c.get("/kids").text


def test_home_card_for_kids_edu_shows_tomorrows_count(env, monkeypatch):
    from pa.core import home_cards
    _, c, con, tmp_path = env
    c.post("/kids/settings", data=FORM)
    monkeypatch.setattr(home_cards, "_today", lambda: date(2026, 10, 12))
    html = c.get("/").text
    assert 'id="card-kids_edu"' in html and "내일 준비물 2가지" in html and 'href="/kids"' in html


def test_home_card_hidden_without_the_interest(con, tmp_path):
    profile.save(tmp_path, {**profile.defaults(), "members": MEMBERS, "interests": []})
    app = create_app(Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path), con, FakeRouter())
    assert 'id="card-kids_edu"' not in owner_client(app).get("/").text


def test_home_card_mentions_pending_notices(env, monkeypatch):
    from pa.core import home_cards
    _, c, con, _ = env
    monkeypatch.setattr(home_cards, "_today", lambda: date(2026, 10, 12))
    con.execute("INSERT INTO notice_event(id,created_at,title,status) VALUES('n','t','행사','new')")
    assert "확인할 가정통신문 1건" in c.get("/").text

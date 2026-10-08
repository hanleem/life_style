"""홈 화면: 고른 관심사의 카드가 먼저, 안 고른 기능은 숨기고 설정에서 켠다."""
import pytest
from conftest import owner_client

from pa.core import home_cards, profile
from pa.settings import Settings
from pa.web.app import create_app


class FakeRouter:
    tone = "polite"
    honorific = ""


def _client(con, tmp_path, **prof):
    profile.save(tmp_path, {**profile.defaults(), **prof})
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path, require_profile=True)
    return owner_client(create_app(s, con, FakeRouter()))


def test_visible_cards_follow_chosen_interests_in_order():
    p = {**profile.defaults(), "interests": ["outing", "housekeeping", "hobby"]}
    assert home_cards.visible(p) == ["outing", "housekeeping"]       # hobby 는 아직 카드가 없다
    assert home_cards.visible({**p, "interests": ["housekeeping", "outing"]}) == ["housekeeping", "outing"]
    assert home_cards.visible({**p, "interests": []}) == []
    assert home_cards.visible(None) == []


def test_home_shows_only_chosen_cards_first(con, tmp_path):
    c = _client(con, tmp_path, interests=["outing"])
    html = c.get("/").text
    assert "주말 나들이" in html and "/outing" in html
    assert "생필품" not in html.split("<main")[1].split("이번 주 식단")[0]


def test_unchosen_cards_are_hidden(con, tmp_path):
    html = _client(con, tmp_path, interests=[]).get("/").text
    assert 'id="card-outing"' not in html and 'id="card-housekeeping"' not in html


def test_interest_card_comes_before_core_cards(con, tmp_path):
    html = _client(con, tmp_path, interests=["housekeeping"]).get("/").text
    assert html.index('id="card-housekeeping"') < html.index("승인 대기")


def test_features_page_lists_every_interest_and_saves(con, tmp_path):
    c = _client(con, tmp_path, interests=["outing"])
    page = c.get("/settings/features").text
    for label in profile.INTERESTS.values():
        assert label in page
    r = c.post("/settings/features", data={"interests": ["housekeeping", "outing"]}, follow_redirects=False)
    assert r.status_code == 303
    assert profile.load(tmp_path)["interests"] == ["housekeeping", "outing"]
    assert 'id="card-housekeeping"' in c.get("/").text


def test_features_page_rejects_unknown_interest(con, tmp_path):
    c = _client(con, tmp_path)
    r = c.post("/settings/features", data={"interests": ["gambling"]}, follow_redirects=False)
    assert r.status_code == 400
    assert profile.load(tmp_path)["interests"] == []


@pytest.mark.parametrize("theme", ["pastel", "wood", "mono", "fresh"])
def test_every_page_carries_the_chosen_theme(con, tmp_path, theme):
    c = _client(con, tmp_path, theme=theme, font_size="large")
    for path in ("/", "/tasks", "/settings"):
        html = c.get(path).text
        assert f'data-theme="{theme}"' in html and 'data-font="large"' in html, path

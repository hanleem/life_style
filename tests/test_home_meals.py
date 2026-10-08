from conftest import owner_client
"""비서 홈의 '이번 주 식단·장보기' 카드: 식단, 쿠팡 장바구니 현황(바로 열기 링크), 생필품, 재고 부족."""
import json
from datetime import date

from fastapi.testclient import TestClient

from pa.core import meals, pantry, supplies
from pa.settings import Settings
from pa.web.app import create_app


def _client(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    c = owner_client(create_app(s, con, None))
    return c


def _plan(con):
    meals.ensure_schema(con)
    con.execute("INSERT OR REPLACE INTO meal_plan(week_start, plan_json, created_at) VALUES('2026-10-10', ?, 't')", (json.dumps({
        "meals": [{"date": "2026-10-10", "slot": "cook", "title": "가지 돼지 미소 덮밥",
                   "ingredients": [{"name": "돼지고기", "qty": "120g"}, {"name": "밥", "qty": "1공기"}]}],
        "shopping": [], "delivery": {}, "key_ingredients": ["가지", "돼지고기"]}, ensure_ascii=False),))


def test_home_card_is_hidden_when_there_is_nothing(con):
    assert meals.home_card(con) is None


def test_home_card_has_plan_cart_link_supplies_and_low_stock(con, tmp_path):
    _plan(con)
    pantry.record_cart(con, "가지", "못생겨도 맛있는 가지 5개입", "https://www.coupang.com/vp/products/6653786138", "5개입")
    pantry.record_cart(con, "두반장", "이금기 중화 두반장 226g", "https://www.coupang.com/vp/products/2436662", "226g")
    supplies.add(con, "화장지")
    pantry.set_staple(con, "즉석밥", 2, 4, "즉석밥,밥")
    card = meals.home_card(con)
    assert card["plan"] and card["key"] == ["가지", "돼지고기"] and card["meals"][0]["kcal"] == round(170 * 1.2 + 150 * 1.3)
    assert [c["name"] for c in card["cart"]] == ["가지", "두반장"] and card["cart_url"].startswith("https://cart.coupang.com/")
    assert card["supplies_wait"] == ["화장지"] and card["low"] == [{"name": "즉석밥", "count": 2, "unit": "개"}]
    html = _client(con, tmp_path).get("/").text
    assert "이번 주 식단·장보기" in html and "가지 돼지 미소 덮밥" in html and "핵심 재료 가지, 돼지고기" in html
    assert "쿠팡 장바구니에 2개 담음" in html and 'href="https://cart.coupang.com/cartView.pang"' in html
    assert 'rel="noopener noreferrer"' in html and "생필품 주문 대기: 화장지" in html and "즉석밥 2개 남음" in html


def test_home_card_never_breaks_the_home_page(con, tmp_path, monkeypatch):
    monkeypatch.setattr(meals, "latest", lambda con_: (_ for _ in ()).throw(RuntimeError("boom")))
    assert meals.home_card(con) is None
    assert _client(con, tmp_path).get("/").status_code == 200

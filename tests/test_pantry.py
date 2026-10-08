from conftest import owner_client
"""재료 보관 기간 DB·집에 있는 재료·유통기한·'좋아요' 상품. 메일·채팅 내용은 다루지 않는다."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from pa.core import meals, pantry
from pa.settings import Settings
from pa.web.app import create_app

D = date(2026, 10, 8)


def test_shelf_life_is_seeded_with_estimates_and_matches_by_name(con):
    assert pantry.shelf_days(con, "오이") == 7 and pantry.shelf_days(con, "소바면") == 270
    assert pantry.shelf_days(con, "국내산 백오이 3개입") == 7           # 이름에 들어 있으면 맞춘다
    assert pantry.shelf_days(con, "처음 보는 재료") == pantry.UNKNOWN_DAYS


def test_bought_item_gets_an_estimated_expiry_then_owner_date_teaches_the_table(con):
    iid = pantry.add_item(con, "가지", "3개", bought_on=D)
    it = pantry.list_items(con, D)[0]
    assert it["expires_on"] == "2026-10-15" and it["estimated"] == 1 and it["days_left"] == 7
    pantry.set_expiry(con, iid, date(2026, 10, 20))                         # 포장의 실제 날짜
    it = pantry.list_items(con, D)[0]
    assert it["expires_on"] == "2026-10-20" and it["estimated"] == 0
    assert pantry.shelf_days(con, "가지") == 12                              # 이 재료의 보관 기간을 실제 값으로 고쳤다(학습)
    row = con.execute("SELECT source FROM shelf_life WHERE name='가지'").fetchone()
    assert row["source"] == "owner"


def test_like_remembers_the_product_for_next_time_and_only_accepts_coupang(con):
    iid = pantry.add_item(con, "두부", "1모", bought_on=D)
    with pytest.raises(ValueError):
        pantry.like_item(con, iid)                                           # 어떤 상품인지 모름
    with pytest.raises(ValueError):
        pantry.like_item(con, iid, "https://evil.example/vp/products/1")
    with pytest.raises(ValueError):
        pantry.like_item(con, iid, "http://www.coupang.com/vp/products/1")   # https 만
    pantry.like_item(con, iid, "https://www.coupang.com/vp/products/123?itemId=9&x=y", "풀무원 두부")
    assert pantry.preferred(con, "두부")["url"] == "https://www.coupang.com/vp/products/123"   # 추적 값은 버린다
    assert pantry.preferred(con, "단단한 두부 또는 순두부")["product_name"] == "풀무원 두부"
    assert pantry.preferred(con, "양파") is None


def test_prompt_block_lists_shelf_life_stock_and_liked_products(con):
    iid = pantry.add_item(con, "브로콜리", bought_on=D)
    pantry.like_item(con, iid, "https://www.coupang.com/vp/products/55")
    text = "\n".join(pantry.prompt_block(con, D))
    assert "금방 상하는 재료" in text and "숙주 3일" in text and "오래 두는 재료" in text
    assert "브로콜리(유통기한 2026-10-14, 추정, 6일 남음)" in text and "같은 상품으로 사는 재료: 브로콜리" in text


def test_meal_prompt_carries_the_shelf_life_block(con):
    meals.ensure_schema(con)
    w = meals.week_window(D)
    from pa.core import profile
    txt = meals._prompt(con, w, meals.load_config(), "일식", profile.defaults())
    assert "[보관 기간" in txt and "금방 상하는 재료" in txt


def _client(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    c = owner_client(create_app(s, con, None))
    return c


def test_pantry_api_and_meals_page_buttons(con, tmp_path):
    c = _client(con, tmp_path)
    assert c.post("/api/pantry", json={"name": "오이", "qty": "3개"}).status_code == 200
    iid = pantry.list_items(con)[0]["id"]
    assert c.post(f"/api/pantry/{iid}/expiry", json={"expires_on": "2099-01-01"}).status_code == 200
    assert c.post(f"/api/pantry/{iid}/like", json={}).status_code == 400
    assert c.post(f"/api/pantry/{iid}/like", json={"product_url": "https://www.coupang.com/vp/products/2233944921", "product_name": "백오이"}).status_code == 200
    assert c.post("/api/pantry", json={"name": "x", "product_url": "https://evil.example/a"}).status_code == 400
    html = c.get("/meals").text
    assert "집에 있는 재료와 유통기한" in html and "👍 좋아요" in html and 'type="date"' in html
    assert c.post("/api/pantry/nope/like", json={"product_url": "https://www.coupang.com/x"}).status_code == 404
    assert c.post(f"/api/pantry/{iid}/remove").status_code == 200 and c.post(f"/api/pantry/{iid}/remove").status_code == 404


def test_home_items_have_no_invented_expiry_and_store_md_is_written(con, tmp_path, monkeypatch):
    import sqlite3
    from pa.db import connect, init_db
    db = connect(tmp_path / "core.db")
    init_db(db)
    pantry.add_home_item(db, "후추")
    pantry.add_home_item(db, "버터", note="곧 다 써야 함", expires_on=date(2026, 10, 15))
    pantry.add_home_item(db, "후추", "병")                                        # 같은 이름은 새로 만들지 않고 갱신
    items = {i["name"]: i for i in pantry.list_items(db, D)}
    assert len(items) == 2 and items["후추"]["expires_on"] is None and items["후추"]["qty"] == "병"
    md = (tmp_path / "store.md").read_text(encoding="utf-8")
    assert "# 우리집 식자재 목록" in md and "후추" in md and "유통기한 모름" in md and "버터 (곧 다 써야 함)" in md
    pantry.remove_item(db, items["후추"]["id"])                                  # 다 쓰면 빠진다
    assert "후추" not in (tmp_path / "store.md").read_text(encoding="utf-8")


def test_generic_shelf_words_classify_dried_frozen_and_condiments_as_long_life(con):
    assert pantry.shelf_days(con, "발사믹") >= pantry.LONG_LIFE_DAYS and pantry.shelf_days(con, "말린 새우") >= pantry.LONG_LIFE_DAYS
    assert pantry.shelf_days(con, "마늘(손질·냉동)") >= pantry.LONG_LIFE_DAYS


def test_cart_product_info_moves_into_stock_when_marked_bought(con):
    pantry.record_cart(con, "즉석밥", "곰곰 작은공기 130g 24개", "https://www.coupang.com/vp/products/8934143282", "24개", date(2027, 3, 9), 24)
    pantry.set_staple(con, "즉석밥", 0, 4, "즉석밥,밥")
    iid = pantry.add_item(con, "즉석밥", bought_on=D)                              # '샀어요'
    it = next(i for i in pantry.list_items(con, D) if i["id"] == iid)
    assert it["expires_on"] == "2027-03-09" and it["estimated"] == 0 and it["product_url"].endswith("/8934143282")
    assert pantry.list_staples(con)[0]["count"] == 24
    assert con.execute("SELECT status FROM cart_item").fetchone()[0] == "bought"


def test_staple_count_drops_per_cooked_meal_once_and_reports_low_stock(con):
    pantry.set_staple(con, "즉석밥", 5, 4, "즉석밥,밥")
    plan = {"week_start": "2026-10-10", "meals": [
        {"date": "2026-10-10", "slot": "cook", "ingredients": [{"name": "즉석밥"}, {"name": "가지"}]},
        {"date": "2026-10-11", "slot": "cook", "ingredients": [{"name": "밥"}]},
        {"date": "2026-10-12", "slot": "lunchbox", "ingredients": [{"name": "면"}]}]}
    assert pantry.consume_staples(con, plan, date(2026, 10, 10)) == 0          # 아직 안 지난 끼니는 빼지 않는다
    assert pantry.consume_staples(con, plan, date(2026, 10, 12)) == 2
    assert pantry.consume_staples(con, plan, date(2026, 10, 13)) == 0          # 같은 끼니는 한 번만
    s = pantry.list_staples(con)[0]
    assert s["count"] == 3 and [x["name"] for x in pantry.low_staples(con)] == ["즉석밥"]


async def test_staples_tick_announces_low_stock_every_three_days_only(con):
    from datetime import datetime, timezone
    from pa import main as pm
    from pa.core import notify
    pantry.set_staple(con, "즉석밥", 2, 4, "즉석밥")
    now = datetime(2026, 10, 12, 3, tzinfo=timezone.utc)
    assert await pm.staples_tick(con, now) == 1
    assert await pm.staples_tick(con, now) == 0                                  # 3일 안에는 다시 알리지 않는다
    titles = [n["title"] for n in notify.unread(con)]
    assert len(titles) == 1 and "즉석밥이(가) 2개 남았어요" in titles[0] and "주문이 필요해요" in titles[0]

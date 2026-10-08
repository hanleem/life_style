"""장보기 도우미: 구매 목록 조회, 장바구니 기록, 결제 요청·로그인 요청·구매 질문은 알림함으로, 매주 목요일 로그인 요청."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from pa import shop_cli
from pa.core import meals, notify, pantry, supplies
from pa.rules import load_rules

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=KST)        # 목
OWNER, CID = 1, 99


def _plan(con):
    meals.ensure_schema(con)
    con.execute("INSERT OR REPLACE INTO meal_plan(week_start, plan_json, created_at) VALUES('2026-10-10', ?, 't')", (json.dumps({
        "meals": [], "key_ingredients": ["가지", "돼지고기"], "delivery": {},
        "shopping": [{"category": "채소", "items": [{"name": "가지", "qty": "4개"}, {"name": "대파", "qty": "1단"}, {"name": "무순 또는 깻잎", "qty": "1팩"}]},
                     {"category": "양념", "items": [{"name": "두반장", "qty": "1병"}]}]}, ensure_ascii=False),))


def test_list_skips_what_is_at_home_or_already_in_the_cart(con):
    _plan(con)
    pantry.add_home_item(con, "대파(냉동)")                                            # 집에 있음
    shop_cli.cart_add(con, "가지", "못생겨도 맛있는 가지 5개입", "https://www.coupang.com/vp/products/1", "5개입")   # 장바구니에 담음
    supplies.add(con, "화장지")
    pantry.set_staple(con, "즉석밥", 1, 4, "즉석밥,밥")
    out = shop_cli.to_buy(con)
    names = [g["name"] for g in out["groceries"]]
    assert names == ["무순 또는 깻잎", "두반장"] and out["supplies"] == ["화장지"]
    assert out["low_staples"] == [{"name": "즉석밥", "count": 1, "unit": "개", "preferred_url": None}]
    assert "결제 금지" in out["rules"] and out["key_ingredients"] == ["가지", "돼지고기"]
    assert list(out)[:4] == ["order", "supplies", "low_staples", "groceries"] and "supplies(생필품)를 일괄로 담고" in out["order"]   # 생필품이 식자재보다 먼저


def test_cart_add_records_the_product_and_moves_a_supply_into_the_cart(con):
    supplies.add(con, "화장지")
    shop_cli.cart_add(con, "화장지", "코디 화장지 30롤", "https://www.coupang.com/vp/products/9", "30롤")
    assert supplies.list_orders(con)[0]["status"] == "in_cart"
    assert con.execute("SELECT product_name FROM cart_item").fetchone()[0] == "코디 화장지 30롤"


async def test_done_login_and_ask_go_to_the_notification_box(con):
    shop_cli.cart_add(con, "가지", "가지", "https://www.coupang.com/vp/products/1", "5개입")
    shop_cli.cart_add(con, "두반장", "두반장", "https://www.coupang.com/vp/products/2", "226g")
    assert await shop_cli.done(con, 55560)
    assert await shop_cli.need_login(con)
    assert await shop_cli.ask(con, "새우 500g을 추가로 담아도 될까요?")
    said = [n["title"] for n in reversed(notify.unread(con))]
    assert "2개를 담았어요" in said[0] and "55,560원" in said[0] and "cart.coupang.com" in said[0] and "결제해 주세요" in said[0]
    assert "로그인" in said[1] and "비밀번호는 제가 입력하지 않아요" in said[1]
    assert said[2].startswith("[구매 확인]") and "새우 500g" in said[2]
    assert not await shop_cli.done(con, 55560)                                         # 같은 날 같은 알림은 한 번만
    assert len(notify.unread(con)) == 3


async def test_weekly_login_request_job_is_scheduled_thursday_after_the_plan_and_posts_once(con):
    t = meals.schedule_times()
    assert t["meal_cart"][0] == 3 and t["meal_plan"][1] < t["meal_cart"][1] < t["meal_grocery"][1] or t["meal_cart"][0] == 3
    await meals.run_job(con, "meal_cart", NOW, load_rules(), None)                      # 식단이 없으면 아무것도 보내지 않는다
    assert notify.unread(con) == []
    _plan(con)
    await meals.run_job(con, "meal_cart", NOW, load_rules(), None)
    await meals.run_job(con, "meal_cart", NOW, load_rules(), None)
    said = [n["title"] for n in notify.unread(con)]
    assert len(said) == 1 and "쿠팡에 로그인해 주세요" in said[0] and "장보기 시간" in said[0]

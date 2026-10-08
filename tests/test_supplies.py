"""생필품 주문 목록: '화장지 구매'라고 적으면 기억하고, 주문이 들어가면 목록을 백지로 새로 시작한다."""
import pytest
from conftest import owner_client

from pa.core import supplies
from pa.db import connect, init_db
from pa.settings import Settings
from pa.web.app import create_app



@pytest.mark.parametrize("text,items", [
    ("화장지 구매", ["화장지"]), ("휴지 사야 해", ["휴지"]), ("샴푸랑 린스 떨어졌어", ["샴푸랑 린스"]),
    ("화장지, 주방세제 주문", ["화장지", "주방세제"]), ("치약이 떨어졌어요", ["치약"]), ("세탁세제 다 썼어", ["세탁세제"]),
    ("메일 확인 구매", []), ("내일 3시 회의 주문", []), ("학교 행사 일정 확인해줘", []), ("이 물건 좀 사줘", []),
    ("", []), ("화장지 구매 " + "가" * 60, [])])
def test_request_parsing(text, items):
    assert supplies.parse_request(text) == items


def test_add_dedupes_and_order_file_is_a_copy_of_the_db(tmp_path):
    db = connect(tmp_path / "core.db")
    init_db(db)
    sid, new = supplies.add(db, "화장지", "큰 것")
    assert new and supplies.add(db, "화장지")[1] is False and supplies.wanted_names(db) == ["화장지"]
    md = (tmp_path / "order_list.md").read_text(encoding="utf-8")
    assert "# 생필품 주문 목록" in md and "화장지 — 큰 것" in md and "백지로 다시 시작" in md
    assert supplies.remove(db, sid) and "- 화장지" not in (tmp_path / "order_list.md").read_text(encoding="utf-8")


def test_order_placed_wipes_ordered_items_but_keeps_ones_not_yet_in_cart(tmp_path):
    db = connect(tmp_path / "core.db")
    init_db(db)
    supplies.add(db, "화장지")
    supplies.add(db, "주방세제")
    assert supplies.mark_in_cart(db, "화장지", "코디 화장지 30롤", "https://www.coupang.com/vp/products/1")
    assert "화장지 — 코디 화장지 30롤" in (tmp_path / "order_list.md").read_text(encoding="utf-8")
    assert supplies.order_placed(db) == 1                                      # 담았던 것(주문됨)만 비운다
    assert supplies.wanted_names(db) == ["주방세제"]
    md = (tmp_path / "order_list.md").read_text(encoding="utf-8")
    assert "- 화장지" not in md and "- 주방세제" in md and "최근에 산 것" not in md   # 지난 기록은 남기지 않는다
    assert supplies.order_placed(db) == 0


def test_meals_page_card_and_api(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    c = owner_client(create_app(s, con, None))
    assert c.post("/api/supplies", json={"name": "화장지"}).json()["new"] is True
    assert c.post("/api/supplies", json={"name": "화장지"}).json()["new"] is False
    html = c.get("/meals").text
    assert "생필품 주문 목록" in html and "화장지" in html and "주문했어요" in html
    oid = supplies.list_orders(con)[0]["id"]
    supplies.mark_in_cart(con, "화장지")
    assert c.post("/api/supplies/order-placed").json()["cleared"] == 1
    assert c.post(f"/api/supplies/{oid}/remove").status_code == 404
    assert c.post("/api/supplies", json={"name": ""}).status_code == 422

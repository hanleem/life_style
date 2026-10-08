"""장보기 도우미 명령: `python -m pa.shop_cli <list|cart-add|done|ask|need-login> ...`

쿠팡 장바구니 담기는 AI 도우미(예약 작업 또는 대화)가 브라우저로 하고, 기록·알림은 이 명령으로 PA 에 남긴다.
- 로그인 정보는 다루지 않는다(사용자가 직접 로그인). 결제·주문 확정은 하지 않는다(장바구니까지).
- 구매에 관한 질문·진행 알림은 앱 알림함으로 남긴다.
- 출력과 알림에는 물건 이름·개수·합계만 담는다(비밀번호, 토큰은 없다)."""
import argparse
import asyncio
import hashlib
import json
import sys
from datetime import date

from pa.core import meals, notify, pantry, supplies
from pa.db import connect


def to_buy(con) -> dict:
    """이번에 장바구니에 담을 것: 식단 구매 목록(이미 집에 있거나 장바구니에 담은 것 제외) + 생필품 주문 대기 + 재고 부족 상비 재료."""
    pantry.ensure_schema(con)
    plan = meals.latest(con)
    have = [i["name"] for i in pantry.list_items(con)]
    carted = [r["name"] for r in con.execute("SELECT name FROM cart_item WHERE status='in_cart'")]

    def covered(name: str, pool: list[str]) -> bool:
        base = name.split(" 또는 ")[0].strip()
        return any(p and (p in base or base in p) for p in pool)
    groceries = []
    for g in (plan or {}).get("shopping") or []:
        for i in g.get("items") or []:
            nm = str(i.get("name") or "")
            if nm and not covered(nm, have) and not covered(nm, carted):
                pref = pantry.preferred(con, nm)
                groceries.append({"name": nm, "qty": i.get("qty") or "", "category": g.get("category"),
                                  "freezable": pantry.is_freezable(nm), "preferred_url": pref["url"] if pref else None})
    low = [{"name": s["name"], "count": s["count"], "unit": s["unit"] or "개", "preferred_url": s["url"]} for s in pantry.low_staples(con)
           if not covered(s["name"], carted)]
    return {"order": "먼저 supplies(생필품)를 일괄로 담고, 그다음 low_staples, 마지막에 groceries(식자재)",
            "supplies": supplies.wanted_names(con), "low_staples": low, "groceries": groceries,
            "week_start": (plan or {}).get("week_start"), "key_ingredients": (plan or {}).get("key_ingredients") or [],
            "rules": "작은 포장·가격 효율 · 핵심 재료는 3~4인분 · 냉동 가능한 것은 대량 · 좋아하는 상품(preferred_url)이 있으면 그것 · 결제 금지"}


def cart_add(con, name: str, product: str, url: str, qty: str = "", expires: str | None = None, count: float | None = None, unit: str = "개") -> str:
    """장바구니에 담은 상품을 기록한다(결제 전). 생필품 주문 대기 항목이면 '장바구니에 담음'으로 바꾼다."""
    cid = pantry.record_cart(con, name, product, url, qty, date.fromisoformat(expires) if expires else None,
                             int(count) if count is not None and float(count).is_integer() else count, unit)
    supplies.mark_in_cart(con, name, product, url)
    return cid


def _count_cart(con) -> int:
    return con.execute("SELECT COUNT(*) FROM cart_item WHERE status='in_cart'").fetchone()[0]


async def done(con, total: int | None = None) -> bool:
    n = _count_cart(con)
    text = (f"쿠팡 장바구니에 {n}개를 담았어요(결제 전{f', 합계 {total:,}원' if total else ''}). 확인하시고 결제해 주세요 → {meals.COUPANG_CART_URL} "
            "결제하신 뒤에는 식단 화면에서 '샀어요', 생필품은 '주문했어요'를 눌러 주세요.")
    return await notify.announce(con, text, f"cart-done-{date.today().isoformat()}")


async def need_login(con) -> bool:
    text = ("쿠팡 로그인이 필요해요. 브라우저에서 쿠팡에 로그인해 주세요(비밀번호는 제가 입력하지 않아요). "
            "로그인하신 뒤 대화창에 '로그인했어'라고 알려 주시면 장바구니를 담을게요.")
    return await notify.announce(con, text, f"cart-login-{date.today().isoformat()}")


async def ask(con, question: str) -> bool:
    """구매에 관한 질문은 알림함으로 묻는다."""
    key = "cart-ask-" + hashlib.sha1(question.encode("utf-8")).hexdigest()[:10] + "-" + date.today().isoformat()
    return await notify.announce(con, "[구매 확인] " + question[:300], key)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pa.shop_cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("cart-add")
    a.add_argument("--name", required=True)
    a.add_argument("--product", required=True)
    a.add_argument("--url", required=True)
    a.add_argument("--qty", default="")
    a.add_argument("--expires")
    a.add_argument("--count", type=float)
    a.add_argument("--unit", default="개")
    d = sub.add_parser("done")
    d.add_argument("--total", type=int)
    sub.add_parser("need-login")
    q = sub.add_parser("ask")
    q.add_argument("question")
    args = p.parse_args(argv)
    from pa.settings import Settings
    s = Settings()
    con = connect(s.core_db_path)
    if args.cmd == "list":
        print(json.dumps(to_buy(con), ensure_ascii=False, indent=1))
    elif args.cmd == "cart-add":
        print(cart_add(con, args.name, args.product, args.url, args.qty, args.expires, args.count, args.unit))
    elif args.cmd == "done":
        print("sent" if asyncio.run(done(con, args.total)) else "not sent")
    elif args.cmd == "need-login":
        print("sent" if asyncio.run(need_login(con)) else "not sent")
    else:
        print("sent" if asyncio.run(ask(con, args.question)) else "not sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())

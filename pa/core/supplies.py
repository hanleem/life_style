"""생필품(화장지·세제 등) 주문 목록. 사용자가 PA 창(비서 대화방·식단 화면)에 '화장지 구매'처럼 적어 두면 기억했다가,
다음 식자재 장보기 때 같이 쿠팡 장바구니에 담는다(결제는 사용자가 직접). 사람이 읽는 사본은 data/order_list.md 로 계속 갱신한다.
메일·채팅 본문은 다루지 않고, 물건 이름과 메모만 저장한다."""
import re
from datetime import date

from pa.clock import utcnow_iso
from pa.ids import new_id

MAX_NAME = 30
# 'OO 구매', 'OO 주문', 'OO 사야 해', 'OO 사줘', 'OO 떨어졌어', 'OO 다 썼어' 처럼 물건 이름 뒤에 사야 한다는 말이 오는 짧은 글
_REQ = re.compile(r"^(?P<item>.{1,60}?)\s*(?:을|를)?\s*(?:구매|주문|사야|사줘|사\s줘|사와|떨어졌|떨어짐|떨어져|다\s?썼|다\s?떨어)\S*(?:\s*\S{0,6})?$")
_NOT_GOODS = ("메일", "논문", "회의", "일정", "미팅", "발표", "과제", "보고서", "식단", "재료", "식자재")
_SPLIT = re.compile(r"\s*(?:,|，|、|/|그리고|및)\s*")


def parse_request(text: str) -> list[str]:
    """생필품 주문 요청이면 물건 이름 목록, 아니면 빈 목록. 길거나 여러 줄인 글, 업무 단어가 든 글은 요청으로 보지 않는다."""
    t = (text or "").strip()
    if not t or len(t) > 50 or "\n" in t:
        return []
    m = _REQ.match(t)
    if not m:
        return []
    names = []
    for part in _SPLIT.split(m.group("item")):
        p = re.sub(r"\s+", " ", part).strip(" .!~")
        p = re.sub(r"(?:이|가|은|는|도)$", "", p) if len(p) > 2 else p
        if (1 <= len(p) <= MAX_NAME and not any(w in p for w in _NOT_GOODS) and not p.isdigit() and p not in names
                and not p.startswith(("이 ", "그 ", "저 "))):
            names.append(p)
    return names


def ensure_schema(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS household_order (id TEXT PRIMARY KEY, name TEXT NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'wanted', "
                "added_at TEXT NOT NULL, in_cart_at TEXT, bought_at TEXT, product_name TEXT, url TEXT)")
    con.commit()


def add(con, name: str, note: str = "") -> tuple[str, bool]:
    """주문 목록에 넣는다. 이미 '주문할 것'에 같은 이름이 있으면 새로 만들지 않는다. (id, 새로 만들었는가)"""
    ensure_schema(con)
    name = re.sub(r"\s+", " ", (name or "")).strip()[:MAX_NAME]
    if not name:
        raise ValueError("물건 이름이 필요합니다")
    row = con.execute("SELECT id FROM household_order WHERE name=? AND status IN ('wanted','in_cart')", (name,)).fetchone()
    if row:
        return row["id"], False
    sid = new_id()
    con.execute("INSERT INTO household_order(id,name,note,status,added_at) VALUES(?,?,?,'wanted',?)", (sid, name, (note or "")[:80], utcnow_iso()))
    con.commit()
    write_order_md(con)
    return sid, True


def list_orders(con, status: tuple[str, ...] = ("wanted", "in_cart")) -> list[dict]:
    ensure_schema(con)
    q = ",".join("?" * len(status))
    return [dict(r) for r in con.execute(f"SELECT id, name, note, status, added_at, in_cart_at, bought_at, product_name, url "
                                         f"FROM household_order WHERE status IN ({q}) ORDER BY added_at", status)]


def wanted_names(con) -> list[str]:
    return [o["name"] for o in list_orders(con, ("wanted",))]


def mark_in_cart(con, name: str, product_name: str | None = None, url: str | None = None) -> bool:
    """쿠팡 장바구니에 담았다고 기록한다(결제 전)."""
    ensure_schema(con)
    n = con.execute("UPDATE household_order SET status='in_cart', in_cart_at=?, product_name=COALESCE(?,product_name), url=COALESCE(?,url) "
                    "WHERE name=? AND status='wanted'", (utcnow_iso(), product_name, url, name)).rowcount
    con.commit()
    write_order_md(con)
    return n > 0


def order_placed(con) -> int:
    """주문이 들어갔다(결제함): 장바구니에 담겨 있던 항목을 모두 비워 목록을 백지로 되돌린다. 아직 장바구니에 담지 않은
    '주문할 것'은 다음 주문을 위해 남긴다. 비운 개수를 돌려준다. 지난 구매 기록은 남기지 않는다."""
    ensure_schema(con)
    n = con.execute("DELETE FROM household_order WHERE status='in_cart'").rowcount
    con.execute("DELETE FROM household_order WHERE status='bought'")
    con.commit()
    write_order_md(con)
    return n


def remove(con, order_id: str) -> bool:
    ensure_schema(con)
    n = con.execute("DELETE FROM household_order WHERE id=?", (order_id,)).rowcount
    con.commit()
    write_order_md(con)
    return n > 0


def _order_path(con):
    from pathlib import Path
    row = next(iter(con.execute("PRAGMA database_list")), None)
    f = row["file"] if row and row["file"] else ""
    return Path(f).parent / "order_list.md" if f else None


def write_order_md(con, path=None) -> str:
    """order_list.md 를 DB 에서 다시 쓴다(DB 가 원본, 이 파일은 사람이 읽는 사본)."""
    ensure_schema(con)
    nl = chr(10)

    def day(v):
        return (v or "")[:10]
    wanted, cart = list_orders(con, ("wanted",)), list_orders(con, ("in_cart",))
    lines = ["# 생필품 주문 목록", "",
             f"_자동으로 갱신됩니다 · 마지막 갱신 {date.today().isoformat()} · PA 창에 '화장지 구매'처럼 적으면 여기에 올라가고, 다음 식자재 장보기 때 같이 쿠팡 장바구니에 담습니다(결제는 직접) · 주문이 들어가면 이 목록은 백지로 다시 시작합니다_", "",
             "## 주문할 것 (다음 장볼 때 같이 담기)"]
    lines += [f"- {o['name']}{' — ' + o['note'] if o['note'] else ''} (추가 {day(o['added_at'])})" for o in wanted] or ["- 없음"]
    lines += ["", "## 쿠팡 장바구니에 담음 (결제 전)"]
    lines += [f"- {o['name']}{' — ' + o['product_name'] if o['product_name'] else ''} ({day(o['in_cart_at'])})" for o in cart] or ["- 없음"]
    lines += [""]
    text = nl.join(lines)
    path = path or _order_path(con)
    if path:
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return text

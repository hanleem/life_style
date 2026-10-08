"""알림 — 허용 kind·고정 문구만. 내용(제목·본문 원문)은 싣지 않는다.

새 알림은 앱 안 알림함(notification 표)에 쌓고, 같은 지점에서 등록한 기기로 웹 푸시(pa.core.push)도 보낸다.
푸시에는 고정 문구 제목과 이동할 화면 주소만 담는다. 푸시가 실패해도 알림 자체는 남는다."""
import json

from pa.clock import utcnow_iso
from pa.core import push
from pa.ids import new_id

ALLOWED = {"approval_request": "/approvals",
           "briefing_morning": "/", "briefing_evening": "/", "decision_needed": "/",
           "system": "/settings/health",
           "routine_supply": "/routine", "routine_close": "/routine/evening", "reminder": "/", "meal_plan": "/meals", "kids_prep": "/kids"}
_TITLES = {"approval_request": "승인 요청 {n}건",
           "briefing_morning": "아침 브리핑", "briefing_evening": "저녁 브리핑",
           "decision_needed": "결정이 필요한 일 {n}건", "system": "비서 점검 필요",
           "routine_supply": "이번 주 주문 확인", "routine_close": "하루 마무리 시간입니다",
           "reminder": "챙길 일이 있습니다", "meal_plan": "이번 주 식단이 나왔어요",
           "kids_prep": "내일 준비물이 있어요"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification (
  id TEXT PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, href TEXT,
  dedupe_key TEXT NOT NULL UNIQUE, read_at TEXT
);
"""


def ensure_schema(con) -> None:
    con.executescript(_SCHEMA)


def render(kind: str, count: int) -> str:
    return _TITLES[kind].format(n=count)


def _store(con, kind: str, title: str, href: str | None, dedupe_key: str) -> bool:
    ensure_schema(con)
    cur = con.execute("INSERT OR IGNORE INTO notification(id,at,kind,title,href,dedupe_key) VALUES(?,?,?,?,?,?)",
                      (new_id(), utcnow_iso(), kind, title, href, dedupe_key))
    return cur.rowcount == 1


async def send_notify(con, kind: str, count: int, dedupe_key: str) -> bool:
    """고정 문구 알림을 알림함에 남긴다. 같은 dedupe_key 는 한 번만(True=새로 남김)."""
    if kind not in ALLOWED:
        raise ValueError(f"notify kind not allowed: {kind}")
    if not _store(con, kind, render(kind, int(count)), ALLOWED[kind], dedupe_key):
        return False
    con.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES(?,?,?,?,?)",
                (new_id(), utcnow_iso(), "pa", "pa_notify",
                 json.dumps({"kind": kind, "count": count, "dedupe": dedupe_key})))
    await push.deliver(con, render(kind, int(count)), ALLOWED[kind])
    return True


async def announce(con, text: str, dedupe_key: str) -> bool:
    """짧은 안내 문장을 알림함에 남긴다(품목 이름·개수 정도의 고정 문구만). 푸시에는 문장을 싣지 않고 '챙길 일' 알림만 보낸다."""
    if not _store(con, "reminder", text[:200], "/", dedupe_key):
        return False
    await push.deliver(con, render("reminder", 1), "/")
    return True


def unread(con, limit: int = 20) -> list[dict]:
    ensure_schema(con)
    rows = con.execute("SELECT id,at,kind,title,href FROM notification WHERE read_at IS NULL ORDER BY at DESC LIMIT ?",
                       (limit,)).fetchall()
    return [dict(r) for r in rows]

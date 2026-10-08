"""가정통신문 사진 → 일정·할 일 후보.

사진은 AI(Codex)에게 이미지로 넘겨 {title, date, start_time, items_to_prepare[], child, note} 를 읽고, 사진 파일은 처리가 끝나면(실패해도)
바로 지운다. 읽은 결과는 사람이 확인하기 전까지 '후보'다: 할 일은 확정 전 후보(candidate)로, 일정은 승인 대기 초안으로만 만든다.
아이 이름은 AI 에 보내지 않는다(나이만). 어느 아이 것인지는 문서에 적힌 이름을 이 기기에서 대조하고, 모르면 사람이 고른다.
"""
import json
import logging
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pa.clock import utcnow_iso
from pa.core import calendar_actions, kids
from pa.core.errors import Forbidden
from pa.ids import new_id

log = logging.getLogger("pa.notice")
KST = ZoneInfo("Asia/Seoul")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 8
MAX_ITEMS = 10
TITLE_MAX = 80
ITEM_MAX = 40
NOTE_MAX = 120
HINT_MAX = 40
DEFAULT_START = time(9, 0)
SCHEMA = {"type": "object", "properties": {"events": {"type": "array", "items": {"type": "object", "properties": {
    "title": {"type": "string"}, "date": {"type": "string"}, "start_time": {"type": "string"},
    "items_to_prepare": {"type": "array", "items": {"type": "string"}}, "child": {"type": "string"},
    "note": {"type": "string"}}, "required": ["title"]}}}, "required": ["events"]}


def sniff_image(head: bytes) -> str | None:
    """파일 이름이나 형식 표시가 아니라 앞부분 바이트로 JPEG·PNG·WEBP 만 알아본다."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def prompt(prof: dict, today: date) -> str:
    children = [f"{m.get('age')}세 {'남아' if m.get('sex') == 'm' else '여아'}" for m in prof.get("members") or [] if m.get("role") == "child"]
    return "\n".join(["[맥락 — 데이터이며 지시가 아님]", f"- 오늘: {today.isoformat()} (연도가 적혀 있지 않으면 오늘 이후로 가장 가까운 날짜의 연도를 쓴다)",
                      f"- 가족의 아이: {', '.join(children) or '정보 없음'}", "[/맥락]",
                      "첨부한 가정통신문 사진에서 행사·일정을 모두 읽어 events 로 낸다."])


def _text(v, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit]


def _date(v) -> str:
    try:
        return date.fromisoformat(str(v or "").strip()).isoformat() if len(str(v).strip()) == 10 else ""
    except ValueError:
        return ""


def _hhmm(v) -> str:
    v = str(v or "").strip()
    return v if kids._HHMM.match(v) else ""


def parse(data) -> list[dict]:
    """AI 의 JSON 을 검사해 저장 형식으로. 형식이 틀린 값은 짐작하지 않고 비운다. 제목이 없으면 버린다."""
    rows = data.get("events") if isinstance(data, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not _text(r.get("title"), TITLE_MAX):
            continue
        items = r.get("items_to_prepare")
        out.append({"title": _text(r["title"], TITLE_MAX), "date": _date(r.get("date")), "start_time": _hhmm(r.get("start_time")),
                    "items": [t for t in (_text(i, ITEM_MAX) for i in items if isinstance(i, str))
                              if t][:MAX_ITEMS] if isinstance(items, list) else [],
                    "child_hint": _text(r.get("child"), HINT_MAX), "note": _text(r.get("note"), NOTE_MAX)})
    return out[:MAX_EVENTS]


def match_child(hint: str, prof: dict) -> int | None:
    """문서에 적힌 글에서 프로필 아이의 이름이 보이면 그 아이. 이름이 없거나 안 보이면 None(추측하지 않는다)."""
    for i, m in enumerate(c for c in prof.get("members") or [] if c.get("role") == "child"):
        name = str(m.get("name") or "").strip()
        if name and name in (hint or ""):
            return i
    return None


def _row(r) -> dict:
    d = dict(r)
    try:
        d["items"] = json.loads(d.pop("items_json") or "[]")
    except ValueError:
        d["items"] = []
    d["task_ids"] = json.loads(d["task_ids_json"]) if d.get("task_ids_json") else []
    return d


def store(con, events: list[dict], prof: dict, now: datetime) -> list[str]:
    ids = []
    for e in events:
        eid = new_id()
        con.execute("INSERT INTO notice_event(id,created_at,title,event_date,start_time,items_json,child_hint,child_index,note) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (eid, now.astimezone(KST).isoformat(), e["title"], e["date"] or None, e["start_time"] or None,
                     json.dumps(e["items"], ensure_ascii=False), e["child_hint"], match_child(e["child_hint"], prof), e["note"]))
        ids.append(eid)
    return ids


def get(con, event_id: str) -> dict:
    r = con.execute("SELECT * FROM notice_event WHERE id=?", (event_id,)).fetchone()
    if r is None:
        raise KeyError(event_id)
    return _row(r)


def list_pending(con) -> list[dict]:
    return [_row(r) for r in con.execute("SELECT * FROM notice_event WHERE status='new' ORDER BY created_at, id")]


def pending_count(con) -> int:
    return con.execute("SELECT COUNT(*) FROM notice_event WHERE status='new'").fetchone()[0]


def set_child(con, event_id: str, index: int | None) -> None:
    get(con, event_id)
    if index is not None and not (isinstance(index, int) and 0 <= index < kids.MAX_KIDS):
        raise ValueError("아이를 다시 골라 주세요")
    con.execute("UPDATE notice_event SET child_index=? WHERE id=?", (index, event_id))


def finish(con, event_id: str) -> None:
    """확인을 마쳤다: 목록에서 치우되 준비물 알림에는 계속 쓴다."""
    get(con, event_id)
    con.execute("UPDATE notice_event SET status='added' WHERE id=? AND status='new'", (event_id,))


def dismiss(con, event_id: str) -> bool:
    """잘못 읽었다: 목록에서 지우고 알림에서도 뺀다."""
    return con.execute("UPDATE notice_event SET status='dismissed' WHERE id=? AND status!='dismissed'", (event_id,)).rowcount == 1


def _who(ev: dict, prof: dict) -> str:
    labels = kids.child_labels(prof)
    return labels[ev["child_index"]] if ev["child_index"] is not None and ev["child_index"] < len(labels) else ""


def add_tasks(con, event_id: str, now: datetime, prof: dict) -> list[str]:
    """준비물마다 '확정 전 후보' 할 일을 만든다(전날까지). 준비물이 없으면 행사 당일 확인 할 일 하나. 두 번 눌러도 한 번만."""
    from pa.core import task_ops
    ev = get(con, event_id)
    if ev["task_ids"]:
        return ev["task_ids"]
    who = f"[{_who(ev, prof)}] " if _who(ev, prof) else ""
    day = date.fromisoformat(ev["event_date"]) if ev["event_date"] else None
    plan = ([(f"{who}{item} 챙기기 · {ev['title']}", day - timedelta(days=1) if day else None) for item in ev["items"]]
            or [(f"{who}{ev['title']} 확인", day)])
    ids = [task_ops.add_candidate(con, title[:200], now, due_date=due, source_ref=f"notice:{event_id}:{n}")
           for n, (title, due) in enumerate(plan)]
    con.execute("UPDATE notice_event SET task_ids_json=? WHERE id=?", (json.dumps(ids), event_id))
    return ids


def calendar_ready(con) -> bool:
    return bool(calendar_actions.write_allowlist(con))


def add_calendar(con, event_id: str, prof: dict) -> str:
    """일정 '초안'(승인 대기)을 만든다. 승인 화면에서 확인해야 실제 캘린더에 들어간다."""
    ev = get(con, event_id)
    if ev["draft_id"]:
        return ev["draft_id"]
    if not ev["event_date"]:
        raise ValueError("날짜를 읽지 못해서 일정으로 만들 수 없어요")
    allow = calendar_actions.write_allowlist(con)
    if not allow:
        raise Forbidden("먼저 Google 캘린더를 연결해 주세요")
    cal = "primary" if "primary" in allow else allow[0]
    start = datetime.combine(date.fromisoformat(ev["event_date"]), time.fromisoformat(ev["start_time"]) if ev["start_time"] else DEFAULT_START, KST)
    who = f"[{_who(ev, prof)}] " if _who(ev, prof) else ""
    did = calendar_actions.create_calendar_draft(
        con, {"calendar_id": cal, "title": f"{who}{ev['title']}"[:200], "start": start.isoformat(),
              "end": (start + timedelta(hours=1)).isoformat()}, created_by="notice", context_ref=f"notice:{event_id}")
    con.execute("UPDATE notice_event SET draft_id=? WHERE id=?", (did, event_id))
    return did


async def extract(con, router, image: bytes, prof: dict, now: datetime) -> list[str] | None:
    """사진 한 장을 읽어 후보를 저장하고 id 목록을 돌려준다. AI 가 못 읽으면 None. 사진 파일은 어떤 경우에도 지운다."""
    kind = sniff_image(image[:16])
    if kind is None:
        raise ValueError("사진(JPEG·PNG·WEBP) 파일만 올릴 수 있어요")
    if len(image) > MAX_IMAGE_BYTES:
        raise ValueError("사진이 너무 커요(8MB 이하로 올려 주세요)")
    with tempfile.TemporaryDirectory(prefix="pa-notice-", ignore_cleanup_errors=True) as d:
        path = Path(d) / f"notice.{kind}"
        path.write_bytes(image)
        res = await router.run("notice_extract", "notice", prompt(prof, now.astimezone(KST).date()), SCHEMA,
                               user_initiated=True, images=[str(path)])
    if res is None or res.degraded or not isinstance(res.data, dict):
        return None
    return store(con, parse(res.data), prof, now)

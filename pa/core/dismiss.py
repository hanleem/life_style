"""오늘 할 일 상자를 옆으로 밀어 '읽음' 처리(숨기기). 중요하지 않거나 이미 한 일을 오늘 할 일에서 치운다.

PA 안에서만 숨긴다: 다른 앱의 원본은 건드리지 않는다. 숨기면 오늘 브리핑에서 바로 빠지고 비어 생긴 자리는
'그 외'에서 올라오며, 이후 브리핑에도 다시 나오지 않는다. 되돌리기를 누르면 다시 나온다."""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from pa.clock import utcnow_iso

log = logging.getLogger("pa.dismiss")
KST = ZoneInfo("Asia/Seoul")
REST_FIELDS = ("text", "source", "reasons", "date")   # 그 외 목록에 보관하는 항목 필드(id 는 따로)


def ensure_schema(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS dismissed_item(key TEXT PRIMARY KEY, label TEXT, item_json TEXT, dismissed_at TEXT NOT NULL)")


def keys(con) -> set[str]:
    ensure_schema(con)
    return {r[0] for r in con.execute("SELECT key FROM dismissed_item")}


def _sections(con, day: str):
    row = con.execute("SELECT id, sections_json FROM briefing WHERE for_date=? ORDER BY CASE kind WHEN 'morning' THEN 0 ELSE 1 END "
                      "LIMIT 1", (day,)).fetchone()
    if not row:
        return None, None
    try:
        return row["id"], json.loads(row["sections_json"] or "{}")
    except ValueError:
        return row["id"], {}


def _save(con, bid: str, sec: dict) -> None:
    con.execute("UPDATE briefing SET sections_json=? WHERE id=?", (json.dumps(sec, ensure_ascii=False, default=str), bid))


def hide(con, key: str, now: datetime) -> dict | None:
    """상자 하나를 숨긴다. 숨긴 항목(없으면 None)을 돌려준다. 같은 키를 두 번 눌러도 안전하다."""
    ensure_schema(con)
    day = now.astimezone(KST).date().isoformat()
    bid, sec = _sections(con, day)
    item = None
    if sec is not None:
        must, rest = list(sec.get("today_must") or []), list(sec.get("rest_items") or [])
        cap_before, from_rest = len(must), False
        for lst in (must, rest):
            hit = next((t for t in lst if t.get("id") == key), None)
            if hit is not None:
                item, from_rest = hit, lst is rest
                lst.remove(hit)
                break
        if item is not None:
            promoted = False
            while len(must) < cap_before and rest:      # 비어 생긴 자리는 '그 외'에서 올린다
                must.append(rest.pop(0))
                promoted = True
            beyond = int(sec.get("rest_count") or 0) - (1 if (from_rest or promoted) else 0)   # '그 외 N건'은 줄어든 만큼만 줄인다
            sec.update(today_must=must, rest_items=rest, rest_count=max(beyond, 0))
            _save(con, bid, sec)
    con.execute("INSERT OR REPLACE INTO dismissed_item(key,label,item_json,dismissed_at) VALUES(?,?,?,?)",
                (key, (item or {}).get("text"), json.dumps(item, ensure_ascii=False, default=str) if item else None, utcnow_iso()))
    con.commit()
    return item


def undo(con, key: str, now: datetime) -> bool:
    """숨기기를 되돌린다. 오늘 브리핑에 다시 넣는다(자리가 있으면 위쪽, 없으면 '그 외'의 맨 앞)."""
    ensure_schema(con)
    row = con.execute("SELECT item_json FROM dismissed_item WHERE key=?", (key,)).fetchone()
    if not row:
        return False
    con.execute("DELETE FROM dismissed_item WHERE key=?", (key,))
    bid, sec = _sections(con, now.astimezone(KST).date().isoformat())
    item = json.loads(row["item_json"]) if row["item_json"] else None
    if sec is not None and item is not None:
        must, rest = list(sec.get("today_must") or []), list(sec.get("rest_items") or [])
        if any(t.get("id") == key for t in must + rest):
            pass
        elif len(must) < 3:
            must.append(item)
        else:
            rest.insert(0, {k: item.get(k) for k in REST_FIELDS} | {"id": key})
            sec["rest_count"] = int(sec.get("rest_count") or 0) + 1
        sec.update(today_must=must, rest_items=rest)
        _save(con, bid, sec)
    con.commit()
    return True


def recent(con, now: datetime, limit: int = 20) -> list[dict]:
    """오늘 숨긴 항목(복원용)."""
    ensure_schema(con)
    return [dict(r) for r in con.execute(
        "SELECT key,label FROM dismissed_item WHERE label IS NOT NULL ORDER BY dismissed_at DESC LIMIT ?", (limit,))]

"""할 일 추가·완료·지우기(되돌리기)·묘비 · 중복 제안·병합.

* 지운 일의 `task_source` 행은 남는다(묘비) — 같은 출처가 다시 들어와도 되살아나지 않는다.
* 자동 병합은 없다. 비슷한 제목은 `suggestions`로 제안만 하고, 사용자가 `merge`를 누를 때만 합친다(D11).
* 감사 기록에는 id만 남긴다.
"""
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pa.core.drafts import audit
from pa.db import atomic
from pa.core.errors import IllegalTransition, NotFound

KST = ZoneInfo("Asia/Seoul")
RESTORE_WINDOW = timedelta(hours=24)
SIMILAR_MIN = 0.6
SUGGEST_MAX_TASKS = 100
SUGGEST_RECENT = timedelta(days=14)
_LIVE = ("candidate", "open")


def _utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _parse(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _task(con, task_id: str):
    row = con.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(task_id)
    return row


def drop(con, task_id: str, now: datetime) -> None:
    """candidate|open -> dropped. task_source 는 지우지 않는다(묘비). 이미 dropped 면 그대로, done 이면 IllegalTransition."""
    t = _task(con, task_id)
    if t["status"] == "dropped":
        return
    if t["status"] not in _LIVE:
        raise IllegalTransition(f"{t['status']} -> dropped")
    with atomic(con):
        con.execute("UPDATE task SET status='dropped', updated_at=? WHERE id=?", (_utc(now), task_id))
        audit(con, "owner", "task.drop", {"task_id": task_id})


def restore(con, task_id: str, now: datetime) -> None:
    """dropped -> candidate (원래 상태를 추측하지 않는다). 지운 지 24시간 안에만."""
    t = _task(con, task_id)
    if t["status"] != "dropped":
        raise IllegalTransition(f"{t['status']} -> candidate")
    at = _parse(t["updated_at"])
    if at is None or now.astimezone(timezone.utc) - at > RESTORE_WINDOW:
        raise IllegalTransition("restore window passed")
    if con.execute("SELECT 1 FROM audit_log WHERE event='task.merge' AND json_extract(detail_json,'$.other')=? LIMIT 1",
                   (task_id,)).fetchone():
        raise IllegalTransition("merged task cannot be restored")   # 병합으로 지워진 일은 되살리면 중복 고아가 된다
    with atomic(con):
        con.execute("UPDATE task SET status='candidate', updated_at=? WHERE id=?", (_utc(now), task_id))
        audit(con, "owner", "task.restore", {"task_id": task_id})


def is_tombstoned(con, source_type: str, source_ref: str) -> bool:
    """이 출처에 연결된 task 가 지워졌는가(그렇다면 다시 등록하지 않는다)."""
    row = con.execute("SELECT t.status FROM task_source s JOIN task t ON t.id=s.task_id "
                      "WHERE s.source_type=? AND s.source_ref=?", (source_type, source_ref)).fetchone()
    return bool(row and row["status"] == "dropped")


def add(con, title: str, now: datetime, *, due_at: str | None = None, all_day: bool = False) -> str:
    """할 일을 직접 추가한다(status=open)."""
    from pa.ids import new_id
    title = (title or "").strip()
    if not title or len(title) > 200:
        raise ValueError("할 일 제목은 1~200자여야 합니다")
    if due_at is not None:
        datetime.fromisoformat(due_at)
    tid, ts = new_id(), _utc(now)
    with atomic(con):
        con.execute("INSERT INTO task(id,title,bucket,due_at,due_is_all_day,due_confidence,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (tid, title, "this_week", due_at, int(all_day), "explicit" if due_at else "unknown", "open", ts, ts))
        audit(con, "owner", "task.add", {"task_id": tid})
    return tid


def complete(con, task_id: str, now: datetime) -> None:
    """candidate|open -> done."""
    t = _task(con, task_id)
    if t["status"] == "done":
        return
    if t["status"] not in _LIVE:
        raise IllegalTransition(f"{t['status']} -> done")
    with atomic(con):
        con.execute("UPDATE task SET status='done', completed_at=?, updated_at=? WHERE id=?",
                    (_utc(now), _utc(now), task_id))
        audit(con, "owner", "task.complete", {"task_id": task_id})


def confirm(con, task_id: str, now: datetime) -> None:
    """candidate -> open (후보 확정)."""
    t = _task(con, task_id)
    if t["status"] == "open":
        return
    if t["status"] != "candidate":
        raise IllegalTransition(f"{t['status']} -> open")
    with atomic(con):
        con.execute("UPDATE task SET status='open', updated_at=? WHERE id=?", (_utc(now), task_id))
        audit(con, "owner", "task.confirm", {"task_id": task_id})


_STRIP_RE = re.compile(r"님")


def _norm(s: str) -> str:
    t = unicodedata.normalize("NFKC", s or "").lower()
    t = _STRIP_RE.sub("", t)
    return "".join(c for c in t if not c.isspace() and unicodedata.category(c)[0] not in ("P", "S"))


def _bigrams(s: str) -> set[str]:
    if not s:
        return set()
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def similar(a: str, b: str) -> float:
    """공백·구두점·'님' 제거 후 문자 bigram Jaccard (0..1)."""
    x, y = _bigrams(_norm(a)), _bigrams(_norm(b))
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def _due_day(t):
    d = _parse(t["due_at"]) if t["due_at"] else None
    return d.astimezone(KST).date() if d else None


def suggestions(con, now: datetime) -> list[tuple[str, str, float]]:
    """합칠 만한 task 쌍 제안. 자동 병합 없음(D11)."""
    cutoff = _utc(now - SUGGEST_RECENT)
    rows = con.execute("SELECT id,title,due_at FROM task WHERE status IN ('candidate','open') AND created_at>=? "
                       "ORDER BY created_at DESC, id LIMIT ?", (cutoff, SUGGEST_MAX_TASKS)).fetchall()
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if _due_day(a) != _due_day(b):
                continue
            s = similar(a["title"], b["title"])
            if s >= SIMILAR_MIN:
                out.append((a["id"], b["id"], s))
    out.sort(key=lambda r: (-r[2], r[0], r[1]))
    return out


def merge(con, keep_id: str, other_id: str, now: datetime) -> None:
    """other 의 출처 링크를 keep 으로 옮기고 other 는 dropped. keep 의 명시 마감 우선."""
    if keep_id == other_id:
        raise IllegalTransition("same task")
    keep, other = _task(con, keep_id), _task(con, other_id)
    if keep["status"] not in _LIVE or other["status"] not in _LIVE:
        raise IllegalTransition("merge needs live tasks")
    ts = _utc(now)
    with atomic(con):
        con.execute("UPDATE task_source SET task_id=? WHERE task_id=?", (keep_id, other_id))
        if keep["due_confidence"] != "explicit" and other["due_confidence"] == "explicit" and other["due_at"]:
            con.execute("UPDATE task SET due_at=?, due_is_all_day=?, due_confidence='explicit', due_evidence=? WHERE id=?",
                        (other["due_at"], other["due_is_all_day"] or 0, other["due_evidence"], keep_id))
        if keep["status"] == "candidate" and other["status"] == "open":
            con.execute("UPDATE task SET status='open' WHERE id=?", (keep_id,))
        con.execute("UPDATE task SET updated_at=? WHERE id=?", (ts, keep_id))
        con.execute("UPDATE task SET status='dropped', updated_at=? WHERE id=?", (ts, other_id))
        audit(con, "owner", "task.merge", {"keep": keep_id, "other": other_id})

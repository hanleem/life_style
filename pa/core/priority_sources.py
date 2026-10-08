"""출처별 수집기 — 브리핑 순위 목록(priority.rank)에 넣을 Item 을 모은다.

출처: 할 일(task, open), 오늘 캘린더 일정, 오래된 승인 대기.
수집기 하나가 실패해도 그 출처만 빠진다(로그에는 예외 타입 이름만).
순위는 구조화된 사실로만 정한다: 제목 문구는 표시용.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pa.core.priority import Item

log = logging.getLogger("pa.priority_sources")
KST = ZoneInfo("Asia/Seoul")

APPROVAL_TYPES = ("calendar_event",)
APPROVAL_MIN_AGE = timedelta(hours=2)
DROP_RULES: dict[str, tuple[str, ...]] = {
    "calendar": ("ended", "cancelled", "all_day_from_ranking_only"),
    "task": ("done_and_dropped", "candidate_count_only"),
    "approvals": ("young_or_non_calendar",),
}

_SANE_MIN = datetime(2000, 1, 2, tzinfo=timezone.utc)
_SANE_MAX = datetime(2100, 1, 1, tzinfo=timezone.utc)


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return d if _SANE_MIN <= d <= _SANE_MAX else None   # 9999-12-31 같은 값은 날짜 계산에서 OverflowError → 날짜 없음으로 본다


def _task_items(con) -> list[Item]:
    out = []
    srcs: dict[str, set[str]] = {}
    for r in con.execute("SELECT task_id, source_type, source_ref FROM task_source"):
        srcs.setdefault(r["task_id"], set()).add(f"{r['source_type']}:{r['source_ref']}")
    for r in con.execute("SELECT * FROM task WHERE status='open' ORDER BY created_at, id"):
        links = set(srcs.get(r["id"], ())) | {f"task:{r['id']}"}
        out.append(Item(key=f"task:{r['id']}", source="task", text=r["title"], due_at=_dt(r["due_at"]),
                        due_all_day=bool(r["due_is_all_day"]), links=frozenset(links), task_id=r["id"],
                        created_at=_dt(r["created_at"])))
    return out


def _event_items(events: list[dict], now: datetime) -> list[Item]:
    today = now.astimezone(KST).date()
    out = []
    for e in events:
        if e.get("all_day") or e.get("id") is None or str(e.get("status") or "").lower() == "cancelled":
            continue
        s, en = _dt(e.get("start")), _dt(e.get("end"))
        if s is None or s.astimezone(KST).date() != today:
            continue
        if (en or s) <= now:   # 이미 끝난 일정
            continue
        out.append(Item(key=f"event:{e['id']}", source="calendar", text=str(e.get("title") or ""), start_at=s, end_at=en or s,
                        links=frozenset({f"calendar_event:{e['id']}"})))
    return out


def _approval_items(con, now: datetime) -> list[Item]:
    cut = (now - APPROVAL_MIN_AGE).astimezone(timezone.utc)
    ph = ",".join("?" * len(APPROVAL_TYPES))
    rows = con.execute(f"SELECT created_at FROM draft WHERE status='review' AND target_type IN ({ph})", APPROVAL_TYPES).fetchall()
    ats = [d for d in (_dt(r["created_at"]) for r in rows) if d is not None and d <= cut]
    if not ats:
        return []
    return [Item(key="approvals", source="approvals", text=f"승인 대기 {len(ats)}건", waiting_since=min(ats))]


def candidate_task_count(con) -> int:
    """확정 전 후보 할 일 수 — 목록 대신 개수 줄로만."""
    return con.execute("SELECT COUNT(*) FROM task WHERE status='candidate'").fetchone()[0]


def collect(con, now: datetime, *, events: list[dict], kind: str = "morning") -> list[Item]:
    """모든 출처의 Item. con=None 이면 DB 출처(task·승인)는 건너뛴다. kind 는 호출 맥락 표시용."""
    steps = [("events", lambda: _event_items(events, now))]
    if con is not None:
        steps += [("tasks", lambda: _task_items(con)), ("approvals", lambda: _approval_items(con, now))]
    items: list[Item] = []
    for name, fn in steps:
        try:
            items += fn()
        except Exception as e:   # 그 출처만 빠진다. 타입 이름만 남긴다.
            log.warning("priority source %s failed (%s)", name, type(e).__name__)
    return items

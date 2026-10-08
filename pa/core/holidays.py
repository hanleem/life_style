"""휴일 캘린더(구글 '대한민국의 휴일')를 읽어 식사 공급 계산에 반영한다. 읽기 전용, 이름은 저장만 한다."""
import logging
from datetime import date, datetime, timedelta

from pa.core import supply
from pa.db import atomic
from pa.logsafe import log_failure

log = logging.getLogger("pa.holidays")
HOLIDAY_CAL_ID = "ko.south_korea#holiday@group.v.calendar.google.com"
PUBLIC_HOLIDAY = "공휴일"
PAST_DAYS, AHEAD_DAYS = 14, 60     # 지난 며칠은 차감 따라잡기용, 앞은 재주문 시점 계산용


def ensure_schema(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS holiday_day(day TEXT PRIMARY KEY, name TEXT, fetched_at TEXT)")


def load(con) -> int:
    """저장된 휴일을 계산에 올린다(서버 시작 직후, 구글 연결 전에도)."""
    ensure_schema(con)
    days = [date.fromisoformat(r[0]) for r in con.execute("SELECT day FROM holiday_day")]
    supply.set_holidays(days)
    return len(days)


def _days_of(ev) -> list[date]:
    start, end = ev.start.date(), ev.end.date()
    if not ev.all_day or end <= start:
        return [start]
    return [start + timedelta(days=i) for i in range((end - start).days)]


async def refresh(con, cal, today: date, now: datetime) -> int:
    """휴일 캘린더를 읽어 저장하고 계산에 반영한다. 실패하면 이전 값을 그대로 둔다."""
    ensure_schema(con)
    lo, hi = today - timedelta(days=PAST_DAYS), today + timedelta(days=AHEAD_DAYS)
    try:
        events = await cal.events_between(datetime.combine(lo, datetime.min.time(), now.tzinfo),
                                          datetime.combine(hi, datetime.min.time(), now.tzinfo))
    except Exception as e:
        log_failure(log, "holiday calendar read failed", e)
        return -1
    # 이 캘린더에는 기념일(국군의날 등)도 있다. 설명 첫 줄이 '공휴일'인 것만 쉬는 날로 쓴다.
    found = {d: ev.title for ev in events if ev.description == PUBLIC_HOLIDAY for d in _days_of(ev)}
    with atomic(con):
        con.execute("DELETE FROM holiday_day WHERE day>=? AND day<?", (lo.isoformat(), hi.isoformat()))
        con.executemany("INSERT OR REPLACE INTO holiday_day(day,name,fetched_at) VALUES(?,?,?)",
                        [(d.isoformat(), n, now.isoformat()) for d, n in found.items()])
    return load(con)

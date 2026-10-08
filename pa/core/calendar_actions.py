import json

from pa.core import drafts
from pa.core.errors import Forbidden
from pa.core.payloads import CalendarEventPayload

_NONE = "쓰기 허용된 캘린더가 없습니다"


def write_allowlist(con) -> list[str]:
    row = con.execute("SELECT value FROM preference WHERE key='calendar.write_ids'").fetchone()
    if not row or not row["value"]:
        return []
    try:
        ids = json.loads(row["value"])
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def assert_calendar_writable(con, calendar_id: str) -> None:
    if calendar_id not in write_allowlist(con):
        raise Forbidden(_NONE)


def create_calendar_draft(con, payload: dict, created_by: str = "agent",
                          context_ref: str | None = None) -> str:
    p = CalendarEventPayload(**{**payload, "kind": "calendar_event"})
    assert_calendar_writable(con, p.calendar_id)
    return drafts.create_draft(con, p, created_by=created_by, context_ref=context_ref)

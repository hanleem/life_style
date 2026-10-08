import pytest
from pydantic import ValidationError

from pa.core.payloads import CalendarEventPayload
from pa.hashing import canonical_json, payload_hash


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": "한"}) == canonical_json({"a": "한", "b": 1}) == '{"a":"한","b":1}'


def test_payload_hash_changes_with_body_and_schedule():
    p = {"body": "x"}
    assert payload_hash(p) != payload_hash({"body": "y"})
    assert payload_hash(p) != payload_hash(p, "2026-10-07T00:00:00+00:00")
    assert len(payload_hash(p)) == 64


def test_calendar_payload_strips_and_validates():
    p = CalendarEventPayload(calendar_id="primary", title="  학교 행사  ",
                             start="2026-10-07T05:00:00+00:00", end="2026-10-07T06:00:00+00:00")
    assert p.title == "학교 행사" and p.kind == "calendar_event"
    with pytest.raises(ValidationError):
        CalendarEventPayload(calendar_id="primary", title="   ",
                             start="2026-10-07T05:00:00+00:00", end="2026-10-07T06:00:00+00:00")
    with pytest.raises(ValidationError):   # 종료가 시작보다 빠름
        CalendarEventPayload(calendar_id="primary", title="x",
                             start="2026-10-07T06:00:00+00:00", end="2026-10-07T05:00:00+00:00")

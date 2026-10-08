from datetime import datetime, timedelta, timezone
from pa.integrations.calendar import CalEvent, FakeCalendar

T0 = datetime(2026, 10, 6, 0, tzinfo=timezone.utc)


async def test_between_and_idempotent_insert():
    f = FakeCalendar()
    f.events = [CalEvent("e1", "primary", "회의", T0 + timedelta(hours=1), T0 + timedelta(hours=2), False)]
    assert [e.id for e in await f.events_between(T0, T0 + timedelta(days=1))] == ["e1"]
    a = await f.insert_idempotent("act1", "primary", "면담", T0, T0 + timedelta(hours=1))
    b = await f.insert_idempotent("act1", "primary", "면담", T0, T0 + timedelta(hours=1))
    assert a == b and len(f.events) == 2
    assert await f.get_by_action("act1") == a


async def test_overlap_boundaries_sorted_and_unknown_action():
    f = FakeCalendar()
    f.events = [
        CalEvent("late", "primary", "b", T0 + timedelta(hours=5), T0 + timedelta(hours=6), False),
        CalEvent("before", "primary", "x", T0 - timedelta(hours=2), T0, False),
        CalEvent("span", "primary", "s", T0 - timedelta(hours=1), T0 + timedelta(hours=1), False),
    ]
    got = await f.events_between(T0, T0 + timedelta(hours=5))
    assert [e.id for e in got] == ["span"]
    got = await f.events_between(T0, T0 + timedelta(days=1))
    assert [e.id for e in got] == ["span", "late"]
    assert await f.get_by_action("nope") is None

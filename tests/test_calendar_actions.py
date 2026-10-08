import json

import pytest
from pydantic import ValidationError

from pa.core import drafts, worker
from pa.core.calendar_actions import create_calendar_draft
from pa.core.errors import Forbidden
from pa.core.payloads import CalendarEventPayload
from pa.integrations.calendar import FakeCalendar
from conftest import approve_latest

P = {"calendar_id": "primary", "title": "면담",
     "start": "2026-10-07T05:00:00+00:00", "end": "2026-10-07T06:00:00+00:00"}


def _allow(con, ids=("primary",)):
    con.execute("INSERT INTO preference(key,value) VALUES('calendar.write_ids',?)", (json.dumps(list(ids)),))


def test_calendar_write_blocked_without_allowlist(con):
    with pytest.raises(Forbidden):
        create_calendar_draft(con, P)
    _allow(con, ["other"])
    with pytest.raises(Forbidden):
        create_calendar_draft(con, P)


@pytest.mark.parametrize("bad", [
    {**P, "title": ""}, {**P, "title": "x" * 201}, {**P, "start": "2026-10-07T05:00:00"},
    {**P, "end": "2026-10-07T05:00:00+00:00"}, {**P, "end": "2026-10-07T04:00:00+00:00"}])
def test_payload_validation(bad):
    with pytest.raises(ValidationError):
        CalendarEventPayload(kind="calendar_event", **{k: v for k, v in bad.items()})


async def test_calendar_insert_once_after_approval(con):
    _allow(con)
    cal = FakeCalendar()
    did = create_calendar_draft(con, P)
    assert drafts.get_draft(con, did)["target_type"] == "calendar_event"
    await worker.run_once(con, calendar=cal)
    assert cal.events == []
    aid = await approve_latest(con, did)
    await worker.run_once(con, calendar=cal); await worker.run_once(con, calendar=cal)
    assert len(cal.events) == 1
    row = con.execute("SELECT kind,status,result_ref FROM action WHERE id=?", (aid,)).fetchone()
    assert (row["kind"], row["status"], row["result_ref"]) == ("calendar_insert", "sent", cal.events[0].id)


async def test_approve_rechecks_allowlist(con):
    _allow(con)
    did = create_calendar_draft(con, P)
    con.execute("UPDATE preference SET value='[]' WHERE key='calendar.write_ids'")
    with pytest.raises(Forbidden):
        await approve_latest(con, did)


async def test_no_calendar_leaves_queued(con):
    _allow(con)
    did = create_calendar_draft(con, P)
    aid = await approve_latest(con, did)
    await worker.run_once(con)
    assert con.execute("SELECT status FROM action WHERE id=?", (aid,)).fetchone()["status"] == "queued"
    cal = FakeCalendar()
    await worker.run_once(con, calendar=cal)
    assert len(cal.events) == 1


async def test_unknown_reconciles_via_get_by_action(con):
    _allow(con)
    cal = FakeCalendar()
    did = create_calendar_draft(con, P)
    aid = await approve_latest(con, did)
    from datetime import datetime
    eid = await cal.insert_idempotent(aid, "primary", "면담", datetime.fromisoformat(P["start"]), datetime.fromisoformat(P["end"]))
    con.execute("UPDATE action SET status='unknown' WHERE id=?", (aid,))
    con.execute("UPDATE draft SET status='unknown' WHERE id=?", (did,))
    assert await worker.reconcile_unknown(con, calendar=cal) == 1
    row = con.execute("SELECT status,result_ref FROM action WHERE id=?", (aid,)).fetchone()
    assert (row["status"], row["result_ref"]) == ("sent", eid) and len(cal.events) == 1

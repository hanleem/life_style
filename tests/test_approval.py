import asyncio

import pytest
from conftest import cal_payload, calendar_draft

from pa.core import drafts
from pa.core.approval import approve
from pa.core.errors import Forbidden, IllegalTransition, StaleApproval
from pa.core.payloads import CalendarEventPayload


def _setup(con, allow=True):
    did = calendar_draft(con, allow=allow)
    return did, drafts.current_version(con, did)["payload_hash"]


async def test_approve_creates_queued_action(con):
    did, h = _setup(con)
    aid = await approve(con, draft_id=did, version=1, hash_prefix=h[:12], approver="pc:loopback", ttl_minutes=30)
    row = con.execute("SELECT status, kind FROM action WHERE id=?", (aid,)).fetchone()
    assert tuple(row) == ("queued", "calendar_insert")
    assert drafts.get_draft(con, did)["status"] == "queued"


async def test_double_approve_returns_same_action(con):
    did, h = _setup(con)
    a1, a2 = await asyncio.gather(
        approve(con, draft_id=did, version=1, hash_prefix=h[:12], approver="pc:loopback", ttl_minutes=30),
        approve(con, draft_id=did, version=1, hash_prefix=h[:12], approver="pc:loopback", ttl_minutes=30))
    assert a1 == a2
    assert con.execute("SELECT COUNT(*) FROM action").fetchone()[0] == 1


async def test_stale_version_rejected(con):
    did, h = _setup(con)
    drafts.edit_draft(con, did, CalendarEventPayload(**{**cal_payload("v2"), "kind": "calendar_event"}), edited_by="user")
    with pytest.raises(StaleApproval):
        await approve(con, draft_id=did, version=1, hash_prefix=h[:12], approver="pc:loopback", ttl_minutes=30)


async def test_wrong_hash_rejected(con):
    did, _ = _setup(con)
    with pytest.raises(StaleApproval):
        await approve(con, draft_id=did, version=1, hash_prefix="deadbeefdead", approver="pc:loopback", ttl_minutes=30)


async def test_calendar_not_writable_blocks_approval(con):
    did, h = _setup(con)
    con.execute("UPDATE preference SET value='[]' WHERE key='calendar.write_ids'")
    with pytest.raises(Forbidden):
        await approve(con, draft_id=did, version=1, hash_prefix=h[:12], approver="pc:loopback", ttl_minutes=30)
    assert con.execute("SELECT COUNT(*) FROM action").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM approval").fetchone()[0] == 0


async def test_cancelled_draft_cannot_be_approved(con):
    did, h = _setup(con)
    drafts.set_status(con, did, "cancelled")
    with pytest.raises(IllegalTransition):
        await approve(con, draft_id=did, version=1, hash_prefix=h[:12], approver="pc:loopback", ttl_minutes=30)

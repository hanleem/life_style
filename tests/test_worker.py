from datetime import datetime, timedelta

import httpx
import pytest
from conftest import approve_latest, calendar_draft
from freezegun import freeze_time

from pa.clock import utcnow
from pa.core import drafts, worker
from pa.integrations.calendar import FakeCalendar


class FlakyCalendar(FakeCalendar):
    """mode: ok | timeout_after_store(저장은 됐지만 응답 없음) | timeout_before_store | boom"""

    def __init__(self, mode="ok"):
        super().__init__()
        self.mode, self.calls = mode, 0

    async def insert_idempotent(self, action_id, calendar_id, title, start, end):
        self.calls += 1
        if self.mode == "timeout_before_store":
            raise ConnectionError("timeout")
        eid = await super().insert_idempotent(action_id, calendar_id, title, start, end)
        if self.mode == "timeout_after_store":
            raise httpx.ReadTimeout("timeout")
        if self.mode == "boom":
            raise KeyError("boom")
        return eid


async def _approved(con, title="가족 모임"):
    did = calendar_draft(con, title)
    aid = await approve_latest(con, did)
    return did, aid


async def test_unapproved_draft_is_never_executed(con):
    cal = FakeCalendar()
    calendar_draft(con)
    assert await worker.run_once(con, calendar=cal) == 0 and cal.events == []


async def test_executed_event_equals_approved_payload(con):
    cal = FakeCalendar()
    did, aid = await _approved(con, "승인한 최종 일정 ✅")
    await worker.run_once(con, calendar=cal)
    assert cal.events[0].title == "승인한 최종 일정 ✅"
    assert con.execute("SELECT status FROM action WHERE id=?", (aid,)).fetchone()["status"] == "sent"
    assert drafts.get_draft(con, did)["status"] == "sent"


async def test_timeout_marks_unknown_and_does_not_repeat(con):
    cal = FlakyCalendar("timeout_after_store")
    did, aid = await _approved(con)
    await worker.run_once(con, calendar=cal)
    await worker.run_once(con, calendar=cal)
    assert cal.calls == 1
    assert drafts.get_draft(con, did)["status"] == "unknown"
    assert await worker.reconcile_unknown(con, calendar=cal) == 1
    assert drafts.get_draft(con, did)["status"] == "sent" and len(cal.events) == 1


async def test_reconcile_not_found_requires_reapproval(con):
    cal = FlakyCalendar("timeout_before_store")
    did, aid = await _approved(con)
    await worker.run_once(con, calendar=cal)
    assert drafts.get_draft(con, did)["status"] == "unknown"
    assert await worker.reconcile_unknown(con, calendar=cal) == 0
    assert drafts.get_draft(con, did)["status"] == "review"
    assert con.execute("SELECT status FROM action WHERE id=?", (aid,)).fetchone()[0] == "failed"


async def test_expired_approval_not_executed(con):
    cal = FakeCalendar()
    did, aid = await _approved(con)
    with freeze_time(utcnow() + timedelta(minutes=31)):
        await worker.run_once(con, calendar=cal)
    assert cal.events == []
    assert con.execute("SELECT status FROM action WHERE id=?", (aid,)).fetchone()[0] == "expired"
    assert drafts.get_draft(con, did)["status"] == "review"


async def test_restart_recovery_then_reconcile_no_duplicate(con):
    cal = FakeCalendar()
    did, aid = await _approved(con)
    await cal.insert_idempotent(aid, "primary", "가족 모임", datetime.fromisoformat("2026-10-07T05:00:00+00:00"),
                                datetime.fromisoformat("2026-10-07T06:00:00+00:00"))   # 실행 직후 프로세스가 죽은 상황
    con.execute("UPDATE action SET status='sending' WHERE id=?", (aid,))
    con.execute("UPDATE draft SET status='sending' WHERE id=?", (did,))
    assert worker.recover_on_startup(con) == 1
    await worker.reconcile_unknown(con, calendar=cal)
    assert len(cal.events) == 1 and drafts.get_draft(con, did)["status"] == "sent"


async def test_cancel_after_approval_prevents_execution(con):
    cal = FlakyCalendar()
    did, aid = await _approved(con)
    drafts.set_status(con, did, "cancelled")
    await worker.run_once(con, calendar=cal)
    assert cal.events == [] and cal.calls == 0
    assert drafts.get_draft(con, did)["status"] == "cancelled"
    assert con.execute("SELECT status FROM action WHERE id=?", (aid,)).fetchone()[0] == "cancelled"
    assert con.execute("SELECT revoke_reason FROM approval").fetchone()[0] == "cancelled"


async def test_unknown_draft_not_editable(con):
    from pa.core.errors import IllegalTransition
    from pa.core.payloads import CalendarEventPayload
    from conftest import cal_payload
    cal = FlakyCalendar("timeout_after_store")
    did, _ = await _approved(con)
    await worker.run_once(con, calendar=cal)
    assert drafts.get_draft(con, did)["status"] == "unknown"
    with pytest.raises(IllegalTransition):
        drafts.edit_draft(con, did, CalendarEventPayload(**cal_payload("새 제목")), "user")


async def test_unexpected_exception_marks_unknown_no_repeat(con):
    cal = FlakyCalendar("boom")
    did, aid = await _approved(con)
    await worker.run_once(con, calendar=cal)
    await worker.run_once(con, calendar=cal)
    assert cal.calls == 1
    assert drafts.get_draft(con, did)["status"] == "unknown"
    assert "KeyError" in con.execute("SELECT last_error FROM action WHERE id=?", (aid,)).fetchone()[0]


async def test_reconcile_continues_after_error(con):
    cal = FlakyCalendar("timeout_after_store")
    did1, a1 = await _approved(con, "하나")
    did2, a2 = await _approved(con, "둘")
    await worker.run_once(con, calendar=cal)
    orig = cal.get_by_action

    async def flaky(action_id):
        if action_id == a1:
            raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://x"), response=httpx.Response(500))
        return await orig(action_id)
    cal.get_by_action = flaky
    assert await worker.reconcile_unknown(con, calendar=cal) == 1
    assert drafts.get_draft(con, did2)["status"] == "sent"
    assert drafts.get_draft(con, did1)["status"] == "unknown"


async def test_no_calendar_client_leaves_queued(con):
    did, aid = await _approved(con)
    assert await worker.run_once(con, calendar=None) == 0
    assert con.execute("SELECT status FROM action WHERE id=?", (aid,)).fetchone()["status"] == "queued"

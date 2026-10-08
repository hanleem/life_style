import json

import pytest
from conftest import cal_payload

from pa.core import drafts
from pa.core.errors import IllegalTransition, NotFound
from pa.core.payloads import CalendarEventPayload


def _p(title="금요일 가족 모임"):
    return CalendarEventPayload(**cal_payload(title))


def test_create_draft_sets_review_and_version1(con):
    did = drafts.create_draft(con, _p(), created_by="secretary")
    d = drafts.get_draft(con, did)
    v = drafts.current_version(con, did)
    assert d["status"] == "review" and d["current_version"] == 1
    assert json.loads(v["payload_json"])["title"] == "금요일 가족 모임"
    assert v["edited_by"] == "agent"


def test_edit_creates_new_version_and_revokes_approvals(con):
    did = drafts.create_draft(con, _p(), created_by="secretary")
    con.execute("INSERT INTO approval(id,draft_id,version,payload_hash,approver,approved_at,expires_at) "
                "VALUES('a1',?,1,'h','pc:loopback','t','t')", (did,))
    con.execute("INSERT INTO action(id,approval_id,draft_id,kind,payload_json,payload_hash,status,created_at,updated_at) "
                "VALUES('x1','a1',?,'calendar_insert','{}','h','queued','t','t')", (did,))
    v = drafts.edit_draft(con, did, _p("수정본"), edited_by="user")
    assert v == 2
    assert drafts.get_draft(con, did)["current_version"] == 2
    assert con.execute("SELECT revoked_at FROM approval WHERE id='a1'").fetchone()[0] is not None
    assert con.execute("SELECT status FROM action WHERE id='x1'").fetchone()[0] == "cancelled"


def test_illegal_transition(con):
    did = drafts.create_draft(con, _p(), created_by="secretary")
    with pytest.raises(IllegalTransition):
        drafts.set_status(con, did, "sent")


def test_cannot_edit_sent_draft(con):
    did = drafts.create_draft(con, _p(), created_by="secretary")
    con.execute("UPDATE draft SET status='sent' WHERE id=?", (did,))
    with pytest.raises(IllegalTransition):
        drafts.edit_draft(con, did, _p("x"), edited_by="user")


def test_get_missing_draft(con):
    with pytest.raises(NotFound):
        drafts.get_draft(con, "nope")


def test_list_drafts_filters_status(con):
    a = drafts.create_draft(con, _p(), created_by="secretary")
    b = drafts.create_draft(con, _p(), created_by="secretary")
    drafts.set_status(con, b, "held")
    assert [r["id"] for r in drafts.list_drafts(con, ("review",))] == [a]

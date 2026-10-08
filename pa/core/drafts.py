import json
import sqlite3
from contextlib import contextmanager

from pa.clock import utcnow_iso
from pa.core.errors import IllegalTransition, NotFound
from pa.core.payloads import DraftPayload
from pa.hashing import payload_hash
from pa.ids import new_id

ALLOWED: set[tuple[str, str]] = {
    ("draft", "review"), ("review", "queued"), ("review", "held"), ("review", "cancelled"),
    ("held", "review"), ("held", "cancelled"),
    ("queued", "sending"), ("queued", "review"), ("queued", "cancelled"),
    ("sending", "sent"), ("sending", "failed"), ("sending", "unknown"),
    ("unknown", "sent"), ("unknown", "review"), ("failed", "review"),
}
EDITABLE = {"draft", "review", "held", "queued", "failed"}


@contextmanager
def tx(con: sqlite3.Connection):
    if con.in_transaction:   # 이미 바깥 트랜잭션 안: 세이브포인트로 중첩한다
        from pa.db import atomic
        with atomic(con):
            yield
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def audit(con: sqlite3.Connection, actor: str, event: str, detail: dict) -> None:
    con.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES(?,?,?,?,?)",
                (new_id(), utcnow_iso(), actor, event, json.dumps(detail, ensure_ascii=False)))


def get_draft(con, draft_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        raise NotFound(draft_id)
    return row


def current_version(con, draft_id: str) -> sqlite3.Row:
    d = get_draft(con, draft_id)
    return con.execute("SELECT * FROM draft_version WHERE draft_id=? AND version=?",
                       (draft_id, d["current_version"])).fetchone()


def _insert_version(con, draft_id: str, version: int, payload: DraftPayload, edited_by: str) -> None:
    pj = payload.model_dump(mode="json")
    con.execute("INSERT INTO draft_version(id,draft_id,version,payload_json,payload_hash,edited_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (new_id(), draft_id, version, json.dumps(pj, ensure_ascii=False), payload_hash(pj),
                 edited_by, utcnow_iso()))


def create_draft(con, payload: DraftPayload, created_by: str, context_ref: str | None = None) -> str:
    did, now = new_id(), utcnow_iso()
    with tx(con):
        con.execute("INSERT INTO draft(id,target_type,status,current_version,created_by,context_ref,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", (did, payload.kind, "review", 1, created_by, context_ref, now, now))
        _insert_version(con, did, 1, payload, "agent")
        audit(con, created_by, "draft.create", {"draft_id": did})
    return did


def edit_draft(con, draft_id: str, payload: DraftPayload, edited_by: str) -> int:
    with tx(con):
        d = get_draft(con, draft_id)
        if d["status"] not in EDITABLE:
            raise IllegalTransition(f"{d['status']} 상태는 수정할 수 없습니다")
        v = d["current_version"] + 1
        _insert_version(con, draft_id, v, payload, edited_by)
        now = utcnow_iso()
        con.execute("UPDATE approval SET revoked_at=?, revoke_reason='edited' WHERE draft_id=? AND revoked_at IS NULL",
                    (now, draft_id))
        con.execute("UPDATE action SET status='cancelled', updated_at=? WHERE draft_id=? AND status='queued'",
                    (now, draft_id))
        con.execute("UPDATE draft SET current_version=?, status='review', updated_at=? WHERE id=?", (v, now, draft_id))
        audit(con, edited_by, "draft.edit", {"draft_id": draft_id, "version": v})
    return v


def set_status(con, draft_id: str, to: str) -> None:
    d = get_draft(con, draft_id)
    if (d["status"], to) not in ALLOWED:
        raise IllegalTransition(f"{d['status']} -> {to}")
    if d["status"] == "queued":
        if con.in_transaction:
            _leave_queued(con, draft_id, to)
        else:
            with tx(con):
                _leave_queued(con, draft_id, to)
        return
    con.execute("UPDATE draft SET status=?, updated_at=? WHERE id=?", (to, utcnow_iso(), draft_id))


def _leave_queued(con, draft_id: str, to: str) -> None:
    now = utcnow_iso()
    if to != "sending":
        con.execute("UPDATE action SET status='cancelled', updated_at=? WHERE draft_id=? AND status='queued'",
                    (now, draft_id))
        con.execute("UPDATE approval SET revoked_at=?, revoke_reason='cancelled' "
                    "WHERE draft_id=? AND revoked_at IS NULL", (now, draft_id))
    con.execute("UPDATE draft SET status=?, updated_at=? WHERE id=?", (to, now, draft_id))


def list_drafts(con, statuses: tuple[str, ...]) -> list[sqlite3.Row]:
    q = "SELECT * FROM draft WHERE status IN (%s) ORDER BY created_at" % ",".join("?" * len(statuses))
    return con.execute(q, statuses).fetchall()

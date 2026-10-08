import json
import logging
from datetime import datetime

import httpx

from pa.clock import utcnow_iso
from pa.core.drafts import audit, tx

log = logging.getLogger("pa.worker")


def _set(con, action_id: str, draft_id: str, a_status: str, d_status: str, expected: tuple[str, ...],
         **extra) -> None:
    now = utcnow_iso()
    sets = ", ".join(f"{k}=?" for k in extra)
    con.execute(f"UPDATE action SET status=?, updated_at=?{', ' + sets if sets else ''} WHERE id=?",
                (a_status, now, *extra.values(), action_id))
    con.execute(f"UPDATE draft SET status=?, updated_at=? WHERE id=? AND status IN ({','.join('?' * len(expected))})",
                (d_status, now, draft_id, *expected))


async def _dispatch(kind: str, action_id: str, p: dict, calendar=None) -> str:
    if kind == "calendar_insert":
        return await calendar.insert_idempotent(action_id, p["calendar_id"], p["title"],
                                                datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"]))
    raise ValueError(f"unknown action kind: {kind}")


async def run_once(con, limit: int = 10, *, calendar=None) -> int:
    now = utcnow_iso()
    skip_cal = " AND ac.kind != 'calendar_insert'" if calendar is None else ""
    rows = con.execute(
        "SELECT ac.*, ap.expires_at, ap.revoked_at FROM action ac JOIN approval ap ON ap.id=ac.approval_id "
        f"WHERE ac.status='queued' AND (ac.not_before IS NULL OR ac.not_before<=?){skip_cal} "
        "ORDER BY ac.created_at LIMIT ?",
        (now, limit)).fetchall()
    if calendar is None and con.execute(
            "SELECT 1 FROM action WHERE status='queued' AND kind='calendar_insert' LIMIT 1").fetchone():
        log.warning("calendar_insert 액션이 대기 중이지만 캘린더 클라이언트가 없습니다 - queued 유지")
    done = 0
    for r in rows:
        if r["revoked_at"] or r["expires_at"] < now:
            with tx(con):
                _set(con, r["id"], r["draft_id"], "expired", "review", ("queued",))
                audit(con, "worker", "action.expired", {"action_id": r["id"]})
            done += 1
            continue
        cur = con.execute("UPDATE action SET status='sending', attempts=attempts+1, updated_at=? "
                          "WHERE id=? AND status='queued'", (now, r["id"]))
        if cur.rowcount == 0:
            continue
        con.execute("UPDATE draft SET status='sending', updated_at=? WHERE id=? AND status='queued'", (now, r["draft_id"]))
        p = json.loads(r["payload_json"])
        try:
            result_ref = await _dispatch(r["kind"], r["id"], p, calendar)
        except httpx.HTTPStatusError as e:
            _set(con, r["id"], r["draft_id"], "unknown", "unknown", ("sending",), last_error=f"http_{e.response.status_code}")
        except Exception as e:
            _set(con, r["id"], r["draft_id"], "unknown", "unknown", ("sending",), last_error=repr(e)[:200])
        else:
            _set(con, r["id"], r["draft_id"], "sent", "sent", ("sending",), result_ref=result_ref)
        audit(con, "worker", "action.attempt", {"action_id": r["id"]})
        done += 1
    return done


def recover_on_startup(con) -> int:
    now = utcnow_iso()
    ids = [r["id"] for r in con.execute("SELECT id FROM action WHERE status='sending'")]
    for aid in ids:
        con.execute("UPDATE action SET status='unknown', last_error='restart', updated_at=? WHERE id=?", (now, aid))
        con.execute("UPDATE draft SET status='unknown', updated_at=? WHERE id=(SELECT draft_id FROM action WHERE id=?)",
                    (now, aid))
    return len(ids)


async def reconcile_unknown(con, *, calendar=None) -> int:
    """결과를 모르는(unknown) 캘린더 등록을 캘린더에서 확인한다. 없으면 검토 대기로 되돌린다."""
    confirmed = 0
    for r in con.execute("SELECT id, draft_id, kind FROM action WHERE status='unknown'").fetchall():
        if r["kind"] != "calendar_insert" or calendar is None:
            continue
        try:
            eid = await calendar.get_by_action(r["id"])
        except Exception as e:
            log.warning("reconcile failed for %s: %r", r["id"], type(e).__name__)
            continue
        if eid is not None:
            _set(con, r["id"], r["draft_id"], "sent", "sent", ("unknown",), result_ref=str(eid))
            confirmed += 1
        else:
            _set(con, r["id"], r["draft_id"], "failed", "review", ("unknown",), last_error="not_delivered")
            con.execute("UPDATE approval SET revoked_at=?, revoke_reason='not_delivered' "
                        "WHERE id=(SELECT approval_id FROM action WHERE id=?)", (utcnow_iso(), r["id"]))
    return confirmed

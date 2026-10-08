import asyncio
import json
from datetime import datetime, timedelta

from pa.clock import utcnow, utcnow_iso
from pa.core import drafts
from pa.core.drafts import audit, tx
from pa.core.errors import IllegalTransition, StaleApproval
from pa.ids import new_id

_ACTION_KIND = {"calendar_event": "calendar_insert"}

_lock = asyncio.Lock()   # 단일 프로세스 — 같은 버튼 동시 클릭 직렬화


def _existing_action(con, draft_id: str, version: int) -> str | None:
    row = con.execute(
        "SELECT ac.id FROM action ac JOIN approval ap ON ap.id=ac.approval_id "
        "WHERE ap.draft_id=? AND ap.version=? AND ap.revoked_at IS NULL "
        "AND ac.status IN ('queued','sending','sent','unknown')", (draft_id, version)).fetchone()
    return row["id"] if row else None


async def approve(con, *, draft_id: str, version: int, hash_prefix: str, approver: str, ttl_minutes: int,
                  scheduled_send_at: str | None = None) -> str:
    """승인 게이트: 사용자가 본 버전·지문과 일치할 때만 실행 대기열(action)에 올린다."""
    async with _lock:
        existing = _existing_action(con, draft_id, version)
        if existing:
            return existing
        d = drafts.get_draft(con, draft_id)
        if d["status"] != "review":
            raise IllegalTransition(f"{d['status']} 상태는 승인할 수 없습니다")
        if d["current_version"] != version:
            raise StaleApproval("새 버전이 있습니다")
        v = drafts.current_version(con, draft_id)
        if len(hash_prefix) < 8 or not v["payload_hash"].startswith(hash_prefix):
            raise StaleApproval("화면의 문안이 최신이 아닙니다")
        payload = json.loads(v["payload_json"])
        kind = payload.get("kind")
        if kind not in _ACTION_KIND:
            raise IllegalTransition(f"지원하지 않는 초안 종류입니다: {kind}")
        if kind == "calendar_event":
            from pa.core.calendar_actions import assert_calendar_writable
            assert_calendar_writable(con, payload["calendar_id"])

        now = utcnow()
        base = datetime.fromisoformat(scheduled_send_at) if scheduled_send_at else now
        expires = (base + timedelta(minutes=ttl_minutes if not scheduled_send_at else 10)).isoformat()
        apid, acid, now_s = new_id(), new_id(), utcnow_iso()
        with tx(con):
            d2 = drafts.get_draft(con, draft_id)
            v2 = drafts.current_version(con, draft_id)
            if (d2["status"] != "review" or d2["current_version"] != version
                    or v2["payload_hash"] != v["payload_hash"]):
                raise StaleApproval("승인하는 동안 문안이 바뀌었습니다")
            con.execute("INSERT INTO approval(id,draft_id,version,payload_hash,approver,approved_at,expires_at,scheduled_send_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (apid, draft_id, version, v["payload_hash"], approver, now_s, expires, scheduled_send_at))
            con.execute("INSERT INTO action(id,approval_id,draft_id,kind,payload_json,payload_hash,status,not_before,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (acid, apid, draft_id, _ACTION_KIND[kind], v["payload_json"], v["payload_hash"], "queued",
                         scheduled_send_at, now_s, now_s))
            drafts.set_status(con, draft_id, "queued")
            audit(con, approver, "draft.approve", {"draft_id": draft_id, "version": version, "action_id": acid})
        return acid

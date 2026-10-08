import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from pa.clock import utcnow, utcnow_iso
from pa.core import briefing_copy, dismiss, drafts, home_cards, meals, notify, priority_sources, profile
from pa.core.approval import approve
from pa.core.errors import (
    Forbidden,
    IllegalTransition,
    NotFound,
    ScopeChanged,
    StaleApproval,
)
from pa.ids import new_id
from pa.web.auth import consume_nonce, require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()
KST = ZoneInfo("Asia/Seoul")
PENDING = ("review", "held", "queued", "unknown", "failed")


def _now() -> datetime:
    return utcnow()


def _err(e: Exception) -> HTTPException:
    if isinstance(e, (StaleApproval, ScopeChanged, IllegalTransition)):
        return HTTPException(409, str(e))
    if isinstance(e, Forbidden):
        return HTTPException(403, str(e))
    if isinstance(e, NotFound):
        return HTTPException(404, "찾을 수 없습니다")
    raise e


def _draft_view(con, row) -> dict:
    v = drafts.current_version(con, row["id"])
    p = json.loads(v["payload_json"])
    base = {"id": row["id"], "status": row["status"], "version": v["version"], "hash": v["payload_hash"][:12],
            "kind": p.get("kind", "calendar_event")}
    s, e = (datetime.fromisoformat(p[k]).astimezone(KST) for k in ("start", "end"))
    return {**base, "channel_name": "캘린더",
            "body": f"일정 추가: {p['title']} ({s:%m/%d %H:%M}–{e:%H:%M} KST)"}


@router.get("/")
async def home(request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    today = _now().astimezone(KST).date().isoformat()
    row = con.execute("SELECT sections_json FROM briefing WHERE kind='morning' AND for_date=?", (today,)).fetchone()
    sec = None
    if row:
        try:
            sec = json.loads(row["sections_json"] or "{}")
        except ValueError:
            sec = {}
    pending = con.execute("SELECT COUNT(*) FROM draft WHERE status='review'").fetchone()[0]
    cards = home_cards.visible(profile.load(request.app.state.settings.data_dir))
    return templates.TemplateResponse(request, "home.html", {
        "interest_cards": cards, "card_data": home_cards.data(con, cards),
        "sec": sec, "must": ((sec or {}).get("today_must") or [])[:3], "rest": (sec or {}).get("rest_count", 0),
        "events": (sec or {}).get("calendar") or [], "cal_note": (sec or {}).get("calendar_note"),
        "pending": pending, "copy": briefing_copy, "meal_card": meals.home_card(con),
        "task_candidates": priority_sources.candidate_task_count(con),
        "notices": notify.unread(con, 5)})


class DismissIn(BaseModel):
    key: str

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z_]{2,20}:[\w:.@\-]{1,100}", v):
            raise ValueError("항목 식별자가 올바르지 않습니다")
        return v


@router.post("/api/dismiss")
async def dismiss_item(body: DismissIn, request: Request, _: str = Depends(require_owner)):
    """오늘 할 일 상자를 옆으로 밀어 읽음 처리(숨기기). 이 앱 안에서만 숨기고 원본은 건드리지 않는다."""
    item = dismiss.hide(request.app.state.con, body.key, _now())
    return {"ok": True, "label": (item or {}).get("text")}


@router.post("/api/dismiss/undo")
async def dismiss_undo(body: DismissIn, request: Request, _: str = Depends(require_owner)):
    return {"ok": dismiss.undo(request.app.state.con, body.key, _now())}


_SOURCE_LABEL = {"task": "할 일", "calendar": "일정", "approvals": "승인"}


@router.get("/today")
async def today_detail(request: Request, _: str = Depends(require_owner)):
    """홈의 '오늘 꼭 할 일' 상자를 누르면 여는 상세 화면: 항목별 이유·출처·마감, 일정."""
    con = request.app.state.con
    today = _now().astimezone(KST).date().isoformat()
    row = con.execute("SELECT sections_json FROM briefing WHERE for_date=? ORDER BY "
                      "CASE kind WHEN 'morning' THEN 0 ELSE 1 END LIMIT 1", (today,)).fetchone()
    try:
        sec = json.loads(row["sections_json"] or "{}") if row else None
    except ValueError:
        sec = {}
    return templates.TemplateResponse(request, "today.html", {"sec": sec, "labels": _SOURCE_LABEL,
                                                              "hidden": dismiss.recent(con, _now())})


@router.get("/approvals")
async def approvals_page(request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    items = [_draft_view(con, r) for r in drafts.list_drafts(con, PENDING)]
    return templates.TemplateResponse(request, "approvals.html", {"items": items})


@router.get("/api/drafts")
async def list_pending(request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    return [_draft_view(con, r) for r in drafts.list_drafts(con, PENDING)]


class ApproveIn(BaseModel):
    version: int
    hash: str
    nonce: str


@router.post("/api/drafts/{did}/approve")
async def approve_draft(did: str, body: ApproveIn, request: Request, approver: str = Depends(require_owner)):
    s = request.app.state
    try:
        consume_nonce(s.con, body.nonce)
        aid = await approve(s.con, draft_id=did, version=body.version, hash_prefix=body.hash,
                            approver=approver, ttl_minutes=s.settings.approval_ttl_minutes)
    except Exception as e:
        raise _err(e)
    return {"action_id": aid}


@router.post("/api/drafts/{did}/hold")
async def hold(did: str, request: Request, _: str = Depends(require_owner)):
    try:
        drafts.set_status(request.app.state.con, did, "held")
    except Exception as e:
        raise _err(e)
    return {"ok": True}


@router.post("/api/drafts/{did}/discard")
async def discard(did: str, request: Request, _: str = Depends(require_owner)):
    """승인 화면의 '보류': 항목을 삭제(취소) 처리한다."""
    con = request.app.state.con
    try:
        d = drafts.get_draft(con, did)
        if d["status"] not in ("review", "held"):
            raise HTTPException(409, "이미 승인되었거나 처리된 항목은 삭제할 수 없습니다")
        drafts.set_status(con, did, "cancelled")
    except HTTPException:
        raise
    except Exception as e:
        raise _err(e)
    con.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES(?,?,?,?,?)",
                (new_id(), utcnow_iso(), "owner", "draft.discard", json.dumps({"draft_id": did})))
    con.commit()
    return {"ok": True}


@router.post("/api/drafts/{did}/cancel")
async def cancel(did: str, request: Request, _: str = Depends(require_owner)):
    try:
        drafts.set_status(request.app.state.con, did, "cancelled")
    except Exception as e:
        raise _err(e)
    return {"ok": True}

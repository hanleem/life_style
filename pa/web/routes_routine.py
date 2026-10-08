"""물품 공급(식사 재고) 라우트. 모든 라우트는 소유자 인증 필수."""
import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from pa.clock import utcnow
from pa.core import coach_copy, routine_plan, supply
from pa.core.drafts import audit
from pa.core.errors import Forbidden, NotFound
from pa.web.auth import consume_nonce, require_owner
from pa.web.ui import make_templates

log = logging.getLogger(__name__)
router = APIRouter()
_templates = make_templates()


def _now() -> datetime:
    return utcnow()


class NonceIn(BaseModel):
    nonce: str


class RespondIn(BaseModel):
    response: str
    value: int | None = None


_R7_ERRORS = (NotFound, ValueError)


def _bad(e: Exception) -> HTTPException:
    if isinstance(e, NotFound):
        return HTTPException(404, "찾을 수 없습니다")
    return HTTPException(422, str(e))


def _nonce_or_403(con, value: str) -> None:
    try:
        consume_nonce(con, value)
    except Forbidden as e:
        raise HTTPException(403, str(e))


def _today_d() -> date:
    return _now().astimezone(supply.KST).date()


class SupplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=60)
    meals_per_workday: StrictInt
    meals_per_weekend_day: StrictInt = 0
    lead_days: StrictInt = 1
    safety_days: StrictInt = 1
    order_url: str | None = Field(default=None, max_length=500)
    order_cutoff: str | None = None
    remaining: StrictInt = 0


class SupplyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=60)
    meals_per_workday: StrictInt | None = None
    meals_per_weekend_day: StrictInt | None = None
    lead_days: StrictInt | None = None
    safety_days: StrictInt | None = None
    order_url: str | None = Field(default=None, max_length=500)
    order_cutoff: str | None = None


class QtyIn(BaseModel):
    qty: StrictInt


class SkipIn(BaseModel):
    for_date: date | None = None
    qty: StrictInt | None = None


class AdjustIn(BaseModel):
    remaining: StrictInt


class CutoffIn(BaseModel):
    cutoff: str | None = None


class PauseIn(BaseModel):
    start: date
    until: date


class RotationIn(BaseModel):
    item_id: str
    label: str = Field(max_length=60)
    url: str = Field(max_length=500)
    notes: str | None = Field(default=None, max_length=200)
    verified: bool = False


class PlanIn(BaseModel):
    key: str
    value: bool | int | str


@router.post("/api/supply")
async def supply_add(body: SupplyIn, request: Request, _: str = Depends(require_owner)):
    try:
        iid = supply.add_item(request.app.state.con, now=_now(), **body.model_dump())
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"id": iid}


@router.post("/api/supply/{item_id}")
async def supply_update(item_id: str, body: SupplyPatch, request: Request, _: str = Depends(require_owner)):
    fields = body.model_dump(exclude_unset=True)
    try:
        supply.update_item(request.app.state.con, item_id, **fields)
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"ok": True}


@router.post("/api/supply/{item_id}/delivered")
async def supply_delivered(item_id: str, body: QtyIn, request: Request, _: str = Depends(require_owner)):
    try:
        out = supply.deliver(request.app.state.con, item_id, body.qty, now=_now())
    except _R7_ERRORS as e:
        raise _bad(e)
    return out


@router.post("/api/supply/{item_id}/skipped")
async def supply_skipped(item_id: str, body: SkipIn, request: Request, _: str = Depends(require_owner)):
    try:
        supply.skip(request.app.state.con, item_id, body.for_date or _today_d(), qty=body.qty, now=_now())
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"ok": True, "remaining": supply.get_item(request.app.state.con, item_id).remaining}


@router.post("/api/supply/{item_id}/adjust")
async def supply_adjust(item_id: str, body: AdjustIn, request: Request, _: str = Depends(require_owner)):
    try:
        supply.adjust(request.app.state.con, item_id, body.remaining, now=_now())
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"ok": True, "remaining": body.remaining}


@router.post("/api/supply/{item_id}/ordered")
async def supply_ordered(item_id: str, request: Request, _: str = Depends(require_owner)):
    try:
        return supply.mark_ordered(request.app.state.con, item_id, now=_now())
    except _R7_ERRORS as e:
        raise _bad(e)


@router.post("/api/supply/{item_id}/cutoff")
async def supply_cutoff(item_id: str, body: CutoffIn, request: Request, _: str = Depends(require_owner)):
    try:
        supply.set_cutoff(request.app.state.con, item_id, body.cutoff)
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"ok": True}


@router.post("/api/supply/{item_id}/snooze")
async def supply_snooze(item_id: str, request: Request, _: str = Depends(require_owner)):
    try:
        nxt = supply.snooze(request.app.state.con, item_id, _today_d(), now=_now())
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"until": nxt.isoformat()}


@router.post("/api/supply/{item_id}/pause")
async def supply_pause(item_id: str, body: PauseIn, request: Request, _: str = Depends(require_owner)):
    try:
        supply.pause(request.app.state.con, item_id, body.start, body.until)
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"ok": True}


@router.post("/api/supply/{item_id}/resume")
async def supply_resume(item_id: str, request: Request, _: str = Depends(require_owner)):
    try:
        supply.resume(request.app.state.con, item_id)
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"ok": True}


@router.post("/api/supply/{item_id}/stale-ack")
async def supply_stale_ack(item_id: str, request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    try:
        supply.get_item(con, item_id)
        supply.mark_stale_prompted(con, item_id, now=_now())
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"ok": True}


@router.post("/api/supply/{item_id}/delete")
async def supply_delete(item_id: str, body: NonceIn, request: Request, who: str = Depends(require_owner)):
    con = request.app.state.con
    _nonce_or_403(con, body.nonce)
    try:
        supply.get_item(con, item_id)
        supply.update_item(con, item_id, active=0)
    except _R7_ERRORS as e:
        raise _bad(e)
    audit(con, who, "supply.delete", {"id": item_id})
    return {"ok": True}


@router.post("/api/rotation")
async def rotation_add(body: RotationIn, request: Request, _: str = Depends(require_owner)):
    if not body.verified:
        raise HTTPException(422, "직접 확인했는지 체크해 주세요")
    try:
        rid = supply.add_rotation(request.app.state.con, body.item_id, label=body.label, url=body.url,
                                  notes=body.notes, now=_now())
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"id": rid}


@router.post("/api/rotation/dismiss")
async def rotation_dismiss(request: Request, _: str = Depends(require_owner)):
    supply.dismiss_rotation(request.app.state.con, _today_d(), now=_now())
    return {"ok": True}


@router.post("/api/proposal/{pid}")
async def proposal_respond(pid: str, body: RespondIn, request: Request, _: str = Depends(require_owner)):
    return await routine_proposal(pid, body, request, _)



def _https(url: str | None) -> str | None:
    return url if url and url.startswith("https://") else None


@router.post("/api/routine/proposal/{pid}")
async def routine_proposal(pid: str, body: RespondIn, request: Request, _: str = Depends(require_owner)):
    try:
        routine_plan.respond(request.app.state.con, pid, body.response, value=body.value, now=_now())
    except _R7_ERRORS as e:
        raise _bad(e)
    return {"ok": True}


def _routine_view(request: Request) -> dict:
    con, now = request.app.state.con, _now()
    today_d = _today_d()
    props = routine_plan.open_proposals(con, now=now)
    lead: dict[str, list] = {}
    for p in props:
        if p["key"].startswith(routine_plan.LEAD_PREFIX):
            lead.setdefault(p["key"][len(routine_plan.LEAD_PREFIX):], []).append(p)
    items = []
    for it in supply.list_items(con):
        items.append({
            "id": it.id, "label": it.label, "remaining": it.remaining, "default_qty": it.meals_per_workday * 5,
            "due": supply.is_due_today(it, today_d), "hint": supply.cutoff_hint(it, now),
            "stale": supply.stale_check_needed(con, it.id, today_d), "order_url": _https(it.order_url),
            "paused_until": it.paused_until if supply.is_paused(it, today_d) else None,
            "proposals": lead.get(it.id, [])})
    return {"items": items, "copy": coach_copy, "rotation": supply.rotation_card(con, today_d), "https": _https}


@router.get("/routine")
async def routine_page(request: Request, _: str = Depends(require_owner)):
    return _templates.TemplateResponse(request, "routine.html", _routine_view(request))


@router.get("/routine/settings")
async def routine_settings_page(request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    return _templates.TemplateResponse(request, "routine_settings.html", {"copy": coach_copy, "items": supply.list_items(con),
                                                                            "https": _https})

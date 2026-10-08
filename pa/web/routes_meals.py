"""주간 식단 화면: 이번 주 메뉴·구매 목록·레서피. 모든 라우트는 소유자 전용이다."""
from datetime import date

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, HTTPException, Request

from pa.clock import utcnow
from pa.core import growth, household, meals, nutrition, pantry, profile, supplies
from pa.web.auth import require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()


@router.get("/meals")
async def meals_page(request: Request, _: str = Depends(require_owner)):
    plan = meals.latest(request.app.state.con)
    con = request.app.state.con
    prof = profile.load(request.app.state.settings.data_dir) or profile.defaults()
    for m in (plan or {}).get("meals") or []:   # 끼니별 어른 1인분 열량 추정(양을 g 으로 읽을 수 있는 주요 재료 기준)
        m["kcal"] = meals.meal_kcal_per_portion(m)
    days = {}
    for m in (plan or {}).get("meals") or []:
        days.setdefault(m.get("date"), []).append(m)
    report = growth.evaluate(plan) if plan else None
    liked = {}
    for g in (plan or {}).get("shopping") or []:
        for i in g.get("items") or []:
            p = pantry.preferred(con, i.get("name") or "")
            if p:
                liked[i["name"]] = p
    resp = templates.TemplateResponse(request, "meals.html", {
        "plan": plan, "days": sorted(days.items()), "slot_label": household.SLOT_LABEL, "growth": report,
        "growth_badge": growth.badge(report) if report else None, "family": household.summary(prof["members"]),
        "use_coupang": "coupang" in (prof.get("shops") or []), "budget": prof["weekly_budget_won"],
        "stock": pantry.list_items(con), "liked": liked, "staples": pantry.list_staples(con),
        "supplies": supplies.list_orders(con)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/api/meals/generate")
async def meals_generate(request: Request, _: str = Depends(require_owner)):
    router_ = request.app.state.router
    if router_ is None:
        raise HTTPException(503, "AI를 사용할 수 없습니다")
    prof = profile.load(request.app.state.settings.data_dir) or profile.defaults()
    plan = await meals.generate(request.app.state.con, router_, utcnow(), user_initiated=True, prof=prof)
    if plan is None:
        raise HTTPException(502, "식단을 만들지 못했습니다. 잠시 뒤 다시 시도해 주세요")
    return {"ok": True, "week_start": plan["week_start"]}


class PantryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    qty: str = Field(default="", max_length=40)
    expires_on: date | None = None
    product_name: str = Field(default="", max_length=120)
    product_url: str = Field(default="", max_length=400)


@router.post("/api/pantry")
async def pantry_add(body: PantryIn, request: Request, _: str = Depends(require_owner)):
    """산 재료를 집에 있는 재료로 넣는다. 유통기한을 비우면 보관 기간 표로 추정해 넣고, 나중에 포장의 날짜를 알려 주면 바로잡는다."""
    try:
        iid = pantry.add_item(request.app.state.con, body.name, body.qty, expires_on=body.expires_on,
                              product_name=body.product_name or None, product_url=body.product_url or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": iid}


class ExpiryIn(BaseModel):
    expires_on: date


@router.post("/api/pantry/{item_id}/expiry")
async def pantry_expiry(item_id: str, body: ExpiryIn, request: Request, _: str = Depends(require_owner)):
    try:
        pantry.set_expiry(request.app.state.con, item_id, body.expires_on)
    except KeyError:
        raise HTTPException(404, "찾을 수 없습니다")
    return {"ok": True}


class StapleIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    count: int = Field(ge=0, le=9999)
    reorder_at: int = Field(default=4, ge=0, le=999)


@router.post("/api/staples")
async def staple_set(body: StapleIn, request: Request, _: str = Depends(require_owner)):
    """개수를 세어 두고 쓰는 재료(예: 즉석밥)의 남은 개수와 '이만큼 남으면 주문' 기준을 고친다."""
    con = request.app.state.con
    cur = next((s for s in pantry.list_staples(con) if s["name"] == body.name), None)
    pantry.set_staple(con, body.name, body.count, body.reorder_at, aliases=(cur or {}).get("aliases") or "", unit=(cur or {}).get("unit") or "개")
    return {"ok": True}


class SupplyIn(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    note: str = Field(default="", max_length=80)


@router.post("/api/supplies")
async def supply_add(body: SupplyIn, request: Request, _: str = Depends(require_owner)):
    """생필품 주문 목록에 넣는다(비서 대화방에 '화장지 구매'라고 적어도 같은 목록에 올라간다)."""
    try:
        sid, new = supplies.add(request.app.state.con, body.name, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": sid, "new": new}


@router.post("/api/supplies/order-placed")
async def supply_order_placed(request: Request, _: str = Depends(require_owner)):
    """주문을 넣었다: 장바구니에 담겨 있던 항목을 비워 목록을 새로 시작한다(아직 안 담은 항목은 남는다)."""
    return {"ok": True, "cleared": supplies.order_placed(request.app.state.con)}


@router.post("/api/supplies/{order_id}/remove")
async def supply_remove(order_id: str, request: Request, _: str = Depends(require_owner)):
    if not supplies.remove(request.app.state.con, order_id):
        raise HTTPException(404, "찾을 수 없습니다")
    return {"ok": True}


class LikeIn(BaseModel):
    product_url: str = Field(default="", max_length=400)
    product_name: str = Field(default="", max_length=120)


@router.post("/api/pantry/{item_id}/like")
async def pantry_like(item_id: str, body: LikeIn, request: Request, _: str = Depends(require_owner)):
    """주문한 상품 상태가 좋았다: 그 재료는 앞으로 이 상품으로 주문한다(식단의 구매 목록에 이 상품 링크가 먼저 나온다)."""
    try:
        r = pantry.like_item(request.app.state.con, item_id, body.product_url or None, body.product_name or None)
    except KeyError:
        raise HTTPException(404, "찾을 수 없습니다")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **r}


@router.post("/api/pantry/{item_id}/remove")
async def pantry_remove(item_id: str, request: Request, _: str = Depends(require_owner)):
    if not pantry.remove_item(request.app.state.con, item_id):
        raise HTTPException(404, "찾을 수 없습니다")
    return {"ok": True}

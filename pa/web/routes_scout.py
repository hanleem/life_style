"""나들이 추천 화면·피드백·취향·설정 라우트. 전부 소유자 전용."""
import json
from datetime import date, datetime, timezone
from typing import Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from pa.core import recs, taste
from pa.core.errors import NotFound
from pa.scout import outing
from pa.scout.config import OVERRIDES_KEY, effective_config, region_is_acceptable
from pa.web.auth import require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()

TAB_LABEL = {"outing": "나들이", "food": "음식"}
REASON_MSG = {
    "budget": "오늘 검색 한도를 다 썼어요",
    "error": "검색 중 오류가 났어요",
    "disabled": "출발 지역을 먼저 설정하세요",
}
FOOD_REASONS = ("음식 별로", "비쌈")
TASTE_DOMAINS = get_args(recs.Domain)
REGION_MSG = "동 단위까지만 입력하세요"
CHIPS = recs.REASON_CHIPS["outing"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- card views (stored data only; no search / LLM) --------------------

def _card(con, rec) -> dict | None:
    r = con.execute("SELECT * FROM outing_suggestion WHERE id=?", (rec["item_ref"],)).fetchone()
    if r is None:
        return None
    last = recs.last_rating(con, rec["id"])
    c = json.loads(r["course_json"])
    w, f = c.get("walk", {}), c.get("food", {})
    return {"id": rec["id"], "domain": "outing", "rating": last[0] if last else None, "chips": CHIPS,
            "title": f"{w.get('name', '')} + {f.get('place', '')}".strip(" +"),
            "sub": c.get("order") or "", "label": outing.sources_label(outing.source_entries(r["sources_json"]))}


def _recent(con, limit: int = 9) -> list[dict]:
    rows = con.execute("SELECT * FROM recommendation WHERE domain='outing' ORDER BY sent_at DESC, id DESC LIMIT ?",
                       (limit,)).fetchall()
    return [c for c in (_card(con, r) for r in rows) if c]


def _rec_url(con, rec) -> str | None:
    r = con.execute("SELECT sources_json FROM outing_suggestion WHERE id=?", (rec["item_ref"],)).fetchone()
    src = outing.source_entries(r["sources_json"]) if r else []
    url = src[0]["url"] if src else None
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


@router.get("/outing")
async def outing_page(request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    return templates.TemplateResponse(request, "outing.html", {"cards": _recent(con)})


@router.post("/api/recs/{rec_id}/open")
async def open_rec(rec_id: str, request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    try:
        rec = recs.get_recommendation(con, rec_id)
    except NotFound:
        raise HTTPException(404, "찾을 수 없습니다")
    url = _rec_url(con, rec)
    if url is None:
        raise HTTPException(404, "열 수 있는 링크가 없습니다")
    recs.mark_opened(con, rec_id, _now())
    return {"url": url}


# ---- run ---------------------------------------------------------------

@router.post("/api/scout/run/outing")
async def run_outing(request: Request, _: str = Depends(require_owner)):
    s = request.app.state
    cfg = effective_config(s.con, s.scout_base)
    s.scout_cfg = cfg
    cards, sr = await outing.build_outing(s.con, s.router, s.http, cfg, _now(), user_initiated=True)
    msg = None
    if sr.degraded:
        msg = REASON_MSG.get(sr.reason or "", REASON_MSG["error"])
    return {"cards": len(cards), "degraded": sr.degraded, "reason": sr.reason if sr.degraded else None, "message": msg}


# ---- feedback ----------------------------------------------------------

class FeedbackIn(BaseModel):
    rec_id: str
    rating: Literal["like", "soso", "dislike"]
    reasons: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=500)


@router.post("/api/feedback")
async def feedback(body: FeedbackIn, request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    try:
        rec = recs.get_recommendation(con, body.rec_id)
    except NotFound:
        raise HTTPException(404, "찾을 수 없습니다")
    now = _now()
    food = None
    if rec["domain"] == "outing":
        food = con.execute("SELECT id FROM recommendation WHERE domain='food' AND item_ref=? "
                           "ORDER BY sent_at DESC, id DESC LIMIT 1", (rec["item_ref"],)).fetchone()
    if food is None:
        taste.apply_feedback(con, body.rec_id, body.rating, body.reasons, body.note, now)
    else:
        taste.apply_feedback(con, body.rec_id, body.rating,
                             [r for r in body.reasons if r not in FOOD_REASONS], body.note, now)
        taste.apply_feedback(con, food["id"], body.rating,
                             [r for r in body.reasons if r in FOOD_REASONS], None, now)
    return {"ok": True}


# ---- taste -------------------------------------------------------------

@router.get("/settings/taste")
async def taste_page(request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    pins = taste._pins(con)
    sections = []
    for d in TASTE_DOMAINS:
        rows = [{"feature": r["feature"], "value": r["value"], "n": r["n"],
                 "pref": "선호" if r["weight"] > 0 else ("비선호" if r["weight"] < 0 else "중립"),
                 "pinned": (d, r["feature"], r["value"]) in pins}
                for r in taste.list_weights(con, d)]
        if rows:
            sections.append({"domain": d, "label": TAB_LABEL.get(d, d), "rows": rows})
    return templates.TemplateResponse(request, "taste.html", {"sections": sections})


def _check_domain(domain: str) -> None:
    if domain not in TASTE_DOMAINS:
        raise HTTPException(422, "알 수 없는 도메인")


@router.delete("/api/taste/{domain}/{feature}/{value:path}")
async def taste_delete(domain: str, feature: str, value: str, request: Request, _: str = Depends(require_owner)):
    _check_domain(domain)
    taste.delete_weight(request.app.state.con, domain, feature, value)
    return {"ok": True}


class PinIn(BaseModel):
    domain: str
    feature: str
    value: str
    pinned: bool = True


@router.post("/api/taste/pin")
async def taste_pin(body: PinIn, request: Request, _: str = Depends(require_owner)):
    _check_domain(body.domain)
    taste.pin_weight(request.app.state.con, body.domain, body.feature, body.value, body.pinned)
    return {"ok": True}


class ResetIn(BaseModel):
    domain: str | None = None


@router.post("/api/taste/reset")
async def taste_reset(request: Request, body: ResetIn | None = None, _: str = Depends(require_owner)):
    if body and body.domain is not None:
        _check_domain(body.domain)
    taste.reset(request.app.state.con, body.domain if body else None)
    return {"ok": True}


# ---- scout settings ----------------------------------------------------

def _overrides(con) -> dict:
    row = con.execute("SELECT value FROM preference WHERE key=?", (OVERRIDES_KEY,)).fetchone()
    try:
        v = json.loads(row["value"]) if row else {}
    except ValueError:
        v = {}
    return v if isinstance(v, dict) else {}


def _save_pref(con, key: str, value) -> None:
    con.execute("INSERT INTO preference (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), _now().isoformat()))


@router.get("/settings/scout")
async def scout_settings_page(request: Request, _: str = Depends(require_owner)):
    con = request.app.state.con
    cfg = effective_config(con, request.app.state.scout_base)
    vac = outing._vacation_dates(con)
    return templates.TemplateResponse(request, "scout_settings.html", {
        "from_region": cfg.outing.from_region or "", "best_lo": cfg.outing.best_oneway_min[0],
        "best_hi": cfg.outing.best_oneway_min[1], "max_oneway": cfg.outing.max_oneway_min,
        "vacation": ", ".join(sorted(vac))})


class ScoutSettingsIn(BaseModel):
    from_region: str | None = None
    best_oneway_min: list[int] | None = None
    max_oneway_min: int | None = Field(default=None, ge=10, le=600)
    vacation_dates: list[str] | None = None


@router.post("/settings/scout")
async def scout_settings_save(body: ScoutSettingsIn, request: Request, _: str = Depends(require_owner)):
    s = request.app.state
    con = s.con
    ov = _overrides(con)
    if body.from_region is not None:
        region = body.from_region.strip()
        if region and (not region_is_acceptable(region) or len(region) > 40):
            raise HTTPException(422, REGION_MSG)
        ov["from_region"] = region
    if body.best_oneway_min is not None:
        b = body.best_oneway_min
        if len(b) != 2 or not (0 < b[0] <= b[1] <= 600):
            raise HTTPException(422, "편도 시간 범위가 올바르지 않습니다")
        ov["best_oneway_min"] = b
    if body.max_oneway_min is not None:
        ov["max_oneway_min"] = body.max_oneway_min
    if body.vacation_dates is not None:
        try:
            dates = sorted({date.fromisoformat(x.strip()).isoformat() for x in body.vacation_dates if x.strip()})
        except ValueError:
            raise HTTPException(422, "날짜는 YYYY-MM-DD 형식이어야 합니다")
        _save_pref(con, "vacation_dates", dates)
    _save_pref(con, OVERRIDES_KEY, ov)
    s.scout_cfg = effective_config(con, s.scout_base)
    return {"ok": True}

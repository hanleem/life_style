"""Outing course (walk + takeout restaurant): normalization and builder (DESIGN §6.3.3, §12)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from pa.core import recs
from pa.core.taste import Candidate, excluded_refs, pin_weight, rank_with_exploration, taste_profile_text
from pa.ids import new_id
from pa.scout.config import ScoutConfig
from pa.scout.card import Card
from pa.scout.features import food_features, outing_features
from pa.scout.search import SearchResult, scout_search
from pa.scout.sources import classify_source
from pa.scout.urlcheck import check_urls, is_specific_item_url

KST = timezone(timedelta(hours=9))
PRICE_UNCHECKED = "가격 확인 필요"
TRAVEL_UNKNOWN = "이동 시간 확인 필요"
BEST_BONUS = 0.5
MAX_SOURCE_URLS = 20
SEEDS_MARKER = "taste_seeds_applied"


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Closure(_M):
    status: Literal["ok", "closed", "unknown"] = "unknown"
    source: str = ""
    checked_at: str = ""


class Drive(_M):
    from_region: str = ""
    stops: list[str] = []
    roundtrip_min_est: list[int] = []
    basis: str = ""


class Walk(_M):
    name: str = ""
    entrance: str = ""
    parking: str = ""
    distance_km: float | None = None
    minutes: int | None = None
    slope: str = ""
    surface: str = ""
    restroom: str = ""
    closure_check: Closure = Closure()


class Price(_M):
    value: str = ""
    checked_at: str = ""


class Food(_M):
    place: str = ""
    cuisine: str = ""
    menu: str = ""
    why_new: str = ""
    price: Price = Price()
    takeout: Literal["yes", "no", "unknown"] = "unknown"
    hours_on_date: str = ""
    break_time: str = ""
    last_order: str = ""
    light_menu: str = ""
    sources: list[str] = []


class Alternative(_M):
    summary: str = ""
    reason: str = ""


class OutingCourse(_M):
    drive: Drive = Drive()
    walk: Walk = Walk()
    food: Food = Food()
    order: str = ""
    alternative: Alternative = Alternative()


def _drop(data: dict, loc: tuple) -> bool:
    cur = data
    for k in loc[:-1]:
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    if isinstance(cur, dict) and loc and loc[-1] in cur:
        del cur[loc[-1]]
        return True
    return False


def _lenient(model: type[BaseModel], data):
    """Validate; drop each offending field (down to the default) until it parses."""
    if not isinstance(data, dict):
        return model()
    data = json.loads(json.dumps(data, default=str))
    for _ in range(50):
        try:
            return model.model_validate(data)
        except ValidationError as e:
            if not any(_drop(data, tuple(err["loc"])) for err in e.errors()):
                return model()
    return model()


def _enforce(c: OutingCourse, official_domains) -> OutingCourse:
    f = c.food
    if f.takeout in ("yes", "no") and not any(
            classify_source(u, official_domains) in ("official", "sns") for u in f.sources):
        f.takeout = "unknown"
    cc = c.walk.closure_check
    if not cc.source:
        cc.status = "unknown"
    if not f.price.checked_at:
        f.price.value = PRICE_UNCHECKED
    return c


def normalize_course(raw: dict, *, from_region: str, official_domains: set[str] = frozenset()) -> OutingCourse:
    c = _lenient(OutingCourse, raw)
    c.drive.from_region = from_region
    return _enforce(c, official_domains)


def source_entries(sources_json: str | None) -> list[dict]:
    """Stored sources: [{url, verify_status, kind}] or a legacy plain URL list."""
    try:
        raw = json.loads(sources_json or "[]")
    except ValueError:
        return []
    out = []
    for x in raw if isinstance(raw, list) else []:
        if isinstance(x, str):
            out.append({"url": x, "verify_status": "unverified", "kind": "other"})
        elif isinstance(x, dict) and isinstance(x.get("url"), str):
            out.append({"url": x["url"], "verify_status": x.get("verify_status") or "unverified",
                        "kind": x.get("kind") or "other"})
    return out


def sources_label(entries: list[dict]) -> str:
    if any(e["verify_status"] == "page_ok" for e in entries):
        return "출처 확인됨(예약·영업은 직접 확인)"
    return "출처 확인 못 함"


def display_takeout(course: OutingCourse) -> str:
    return {"yes": "포장 가능(출처 확인)", "no": "포장 불가"}.get(course.food.takeout, "포장 가능 여부 확인 필요")


# ---- allowed one-way limit ------------------------------------------------

def _kst_week_days(now: datetime) -> list:
    d = now.astimezone(KST).date()
    mon = d - timedelta(days=d.weekday())
    return [mon + timedelta(days=i) for i in range(7)]


def _vacation_dates(con) -> set[str]:
    row = con.execute("SELECT value FROM preference WHERE key='vacation_dates'").fetchone()
    if row is None:
        return set()
    try:
        v = json.loads(row["value"])
    except ValueError:
        return set()
    return {x for x in v if isinstance(x, str)} if isinstance(v, list) else set()


def _rest_streak(con, now: datetime) -> int:
    vac = _vacation_dates(con)
    best = run = 0
    for d in _kst_week_days(now):
        if d.weekday() == 6 or d.isoformat() in vac:  # Sunday always rest; holidays on weekdays are not
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _capacity_extension_reason(con, cfg: ScoutConfig, now: datetime) -> str | None:
    """Hook for the capacity-based extension ("기분 회복용"); implemented in a later task."""
    return None


def allowed_oneway_max(con, cfg: ScoutConfig, now: datetime) -> tuple[int, str | None]:
    o = cfg.outing
    if _rest_streak(con, now) >= o.extend_rest_streak:
        return o.extended_oneway_min, "연휴"
    reason = _capacity_extension_reason(con, cfg, now)
    if reason:
        return o.extended_oneway_min, reason
    return o.max_oneway_min, None


# ---- builder ---------------------------------------------------------------

def _for_week(now: datetime) -> str:
    y, w, _ = now.astimezone(KST).isocalendar()
    return f"{y}-W{w:02d}"


def build_query(cfg: ScoutConfig, hi: int) -> str:
    lo = cfg.outing.best_oneway_min[0]
    return (f"{cfg.outing.from_region} 출발 차로 편도 {lo}~{hi}분 아이와 함께 걷기 좋은 산책 코스 + 포장 가능한 식당"
            " (식당은 샐러드·샌드위치 등 가벼운 메뉴 포함)")


def _apply_taste_seeds(con, cfg: ScoutConfig, now: datetime) -> None:
    if not cfg.taste_seeds:
        return
    if con.execute("SELECT 1 FROM preference WHERE key=?", (SEEDS_MARKER,)).fetchone():
        return
    existing = con.execute("SELECT value FROM preference WHERE key='taste_pins'").fetchone()
    have = {tuple(x) for x in json.loads(existing["value"])} if existing else set()
    for s in cfg.taste_seeds:
        key = (s.get("domain"), s.get("feature"), str(s.get("value")))
        if None not in key and key not in have:
            pin_weight(con, *key, True, now)
    con.execute("INSERT INTO preference (key, value, updated_at) VALUES (?,?,?)",
                (SEEDS_MARKER, "true", now.isoformat()))
    con.commit()


def _oneway_est(c: OutingCourse) -> float | None:
    est = c.drive.roundtrip_min_est
    return max(est) / 2 if est else None


def _best_bonus(c: Candidate) -> float:
    return BEST_BONUS if c.features.get("oneway_bucket") == "30-60" else 0.0


async def build_outing(con, router, http: httpx.AsyncClient, cfg: ScoutConfig, now: datetime,
                       *, user_initiated: bool) -> tuple[list[Card], SearchResult]:
    if not cfg.outing.enabled:
        return [], SearchResult([], True, "disabled")
    region = str(cfg.outing.from_region).strip()
    _apply_taste_seeds(con, cfg, now)
    hi, _reason = allowed_oneway_max(con, cfg, now)
    query = build_query(cfg, hi)
    taste = taste_profile_text(con, "outing") + "\n" + taste_profile_text(con, "food")
    sr = await scout_search(router, "outing", query, taste, user_initiated=user_initiated)
    if sr.degraded:
        return [], sr
    official = set(cfg.outing.official_domains)
    excl_food = excluded_refs(con, "food", now)
    courses: list[OutingCourse] = []
    for item in sr.items:
        c = normalize_course(item, from_region=region, official_domains=official)
        est = _oneway_est(c)
        if est is not None and est > hi:
            continue
        if ("place", c.food.place) in excl_food:
            continue
        courses.append(c)
    for c in courses:
        c.food.sources = [u for u in c.food.sources if is_specific_item_url(u)]
        if c.walk.closure_check.source and not is_specific_item_url(c.walk.closure_check.source):
            c.walk.closure_check.source = ""
    urls = [sorted({*c.food.sources, *([c.walk.closure_check.source] if c.walk.closure_check.source else [])})
            for c in courses]
    flat = sorted({u for us in urls for u in us})[:MAX_SOURCE_URLS]
    checks = dict(zip(flat, await check_urls(http, flat)))
    urls = [[u for u in us if u in checks] for us in urls]
    cands: list[Candidate] = []
    for c, us in zip(courses, urls):
        failed = {u for u in us if checks[u].verify_status == "page_failed"}
        c.food.sources = [u for u in c.food.sources if u in checks and u not in failed]
        if c.walk.closure_check.source in failed:
            c.walk.closure_check.source = ""
        left = [u for u in us if u not in failed]
        if not left:
            continue
        _enforce(c, official)
        d = c.model_dump()
        feats = outing_features(d)
        cands.append(Candidate(c.walk.name or c.food.place, feats,
                               {"course": c, "dump": d, "sources": left, "checks": checks}))
    picked = rank_with_exploration(con, "outing", cands, now, k=2, bonus=_best_bonus)
    week = _for_week(now)
    cards: list[Card] = []
    for cand, explore in picked:
        c: OutingCourse = cand.payload["course"]
        sources = cand.payload["sources"]
        entries = [{"url": u, "verify_status": cand.payload["checks"][u].verify_status,
                    "kind": classify_source(u, official)} for u in sources]
        sid = new_id()
        con.execute(
            "INSERT INTO outing_suggestion (id, for_week, course_json, sources_json, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (sid, week, json.dumps(cand.payload["dump"], ensure_ascii=False),
             json.dumps(entries, ensure_ascii=False), "proposed", now.isoformat()))
        con.commit()
        rid = recs.record_recommendation(con, domain="outing", item_ref=sid, features=cand.features,
                                         query_used=query, now=now)
        recs.record_recommendation(con, domain="food", item_ref=sid, features=food_features(cand.payload["dump"]),
                                   query_used=query, now=now)
        est = _oneway_est(c)
        travel = f"편도 약 {int(est)}분" if est is not None else TRAVEL_UNKNOWN
        reason = f"{travel} · {c.walk.name} 산책 후 {c.food.place} 포장 · {display_takeout(c)}"
        if explore:
            reason = "새로운 추천 · " + reason
        cards.append(Card(rid, "outing", f"{c.walk.name} + {c.food.place}".strip(" +"), sources[0], reason,
                          sources_label(entries), explore,
                          {"suggestion_id": sid, "walk": c.walk.name, "food": c.food.place,
                           "takeout": c.food.takeout, "order": c.order,
                           "alternative": c.alternative.summary}))
    return cards, sr

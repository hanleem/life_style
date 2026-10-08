"""주간 점심 식단·구매 목록·레서피. 목요일에 만들어 폰으로 알리고(금·토 주문 알림), 내용은 PA 화면에서 본다.
알림에는 내용을 싣지 않는다(고정 문구 routine_supply). 취향·집에 있는 재료는 config/meals.yaml."""
import functools
import json
import logging
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from pa.clock import utcnow_iso
from pa.core import fallback_meals, notify, nutrition
from pa.core import supplies as supplies_mod
from pa.core import pantry as pantry_mod
from pa.core import supply

log = logging.getLogger("pa.meals")
KST = ZoneInfo("Asia/Seoul")
CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "meals.yaml"
RECENT_PLANS = 4
SCHEMA = {"type": "object", "properties": {
    "meals": {"type": "array", "items": {"type": "object", "properties": {
        "date": {"type": "string"}, "slot": {"enum": ["cook", "lunchbox"]}, "title": {"type": "string"},
        "why_new": {"type": "string"}, "time_min": {"type": "integer"}, "servings": {"type": "integer"},
        "ingredients": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "qty": {"type": "string"}}}},
        "steps": {"type": "array", "items": {"type": "string"}}}}},
    "shopping": {"type": "array", "items": {"type": "object", "properties": {
        "category": {"type": "string"}, "items": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "qty": {"type": "string"}}}}}}},
    "key_ingredients": {"type": "array", "items": {"type": "string"}},
    "delivery": {"type": "object", "properties": {"count": {"type": "integer"}, "note": {"type": "string"}}}},
    "required": ["meals", "shopping"]}


def load_config(path: Path | None = None) -> dict:
    p = Path(path or CONFIG_PATH)
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_SCHEDULE_KEYS = {"plan": "meal_plan", "cart_login": "meal_cart", "grocery_reminder": "meal_grocery", "delivery_reminder": "meal_delivery"}


@functools.lru_cache(maxsize=1)
def schedule_times() -> dict[str, tuple[int, time]]:
    """작업 이름 -> (요일, 시각). config/meals.yaml 의 schedule 에서 읽는다."""
    out: dict[str, tuple[int, time]] = {}
    for key, job in _SCHEDULE_KEYS.items():
        v = ((load_config().get("schedule") or {}).get(key)) or {}
        try:
            hh, mm = str(v["time"]).split(":")
            out[job] = (_DAYS[str(v["day"]).lower()], time(int(hh), int(mm)))
        except (KeyError, ValueError):
            log.warning("meals schedule %s is invalid; skipped", key)
    return out


def ensure_schema(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS meal_plan(week_start TEXT PRIMARY KEY, plan_json TEXT NOT NULL, created_at TEXT)")


def week_window(today: date) -> dict:
    """다가오는 토요일부터의 한 주. 토·일은 직접 요리, 월요일은 일요일에 만든 도시락(휴일이면 요리),
    화요일부터는 배달(화~금 평일 수 + 토요일 반나절 + 여유 1)."""
    sat = today + timedelta(days=(5 - today.weekday()) % 7)
    sun, mon = sat + timedelta(days=1), sat + timedelta(days=2)
    cook = [sat, sun]
    lunchbox = mon if supply.is_meal_day(mon) else None
    if lunchbox is None:
        cook.append(mon)
    tue = sat + timedelta(days=3)
    delivery = sum(1 for k in range(4) if supply.is_meal_day(tue + timedelta(days=k))) + 2
    return {"week_start": sat, "cook_days": cook, "lunchbox": lunchbox, "delivery_from": tue, "delivery_count": delivery}


def latest(con, week_start: date | None = None) -> dict | None:
    ensure_schema(con)
    if week_start is not None:
        row = con.execute("SELECT * FROM meal_plan WHERE week_start=?", (week_start.isoformat(),)).fetchone()
    else:
        row = con.execute("SELECT * FROM meal_plan ORDER BY week_start DESC LIMIT 1").fetchone()
    if not row:
        return None
    plan = json.loads(row["plan_json"])
    return {**plan, "week_start": row["week_start"], "created_at": row["created_at"]}


def _recent_titles(con, before: str) -> list[str]:
    out: list[str] = []
    for r in con.execute("SELECT plan_json FROM meal_plan WHERE week_start<? ORDER BY week_start DESC LIMIT ?",
                         (before, RECENT_PLANS)):
        out += [m.get("title", "") for m in json.loads(r["plan_json"]).get("meals", []) if m.get("title")]
    return out


_CUISINE_KEYS = ("cuisine_base", "cuisine_variety", "variety_weeks_of_month")
HEALTHY_MOOD = "완전 건강식(기름·소금은 적게, 채소 많이, 맵지 않게)"
VARIETY_MOOD = "조금 매운 요리도 좋음(새로운 맛 위주, 채소는 충분히)"


def recent_cuisines(con, before: str) -> list[str]:
    out: list[str] = []
    for r in con.execute("SELECT plan_json FROM meal_plan WHERE week_start<? ORDER BY week_start DESC LIMIT ?",
                         (before, RECENT_PLANS)):
        c = json.loads(r["plan_json"]).get("cuisine")
        if c:
            out.append(c)
    return out


def week_of_month(d: date) -> int:
    return (d.day - 1) // 7 + 1


def is_variety_week(week_start: date, cfg: dict) -> bool:
    p = cfg.get("prefs") or {}
    return bool(p.get("cuisine_variety")) and week_of_month(week_start) in set(p.get("variety_weeks_of_month") or [])


def pick_cuisine(con, week_start: date, cfg: dict) -> str:
    """건강식 주(1·3·5주)는 기본 요리, 변화 주(2·4주)는 최근에 쓰지 않은 종류 중 무작위(같은 주는 항상 같은 선택)."""
    p = cfg.get("prefs") or {}
    base, pool = p.get("cuisine_base") or "가정식 한식", [str(c) for c in (p.get("cuisine_variety") or [])]
    if not is_variety_week(week_start, cfg):
        return base
    recent = set(recent_cuisines(con, week_start.isoformat()))
    return random.Random(week_start.toordinal()).choice(sorted(c for c in pool if c not in recent) or sorted(pool))


def _prompt(con, w: dict, cfg: dict, cuisine: str) -> str:
    prefs, pantry = cfg.get("prefs") or {}, list(cfg.get("pantry") or [])
    pantry += [i["name"] for i in pantry_mod.list_items(con) if i["name"] not in pantry]   # 식단 화면·store.md 의 재고도 구매 목록에서 뺀다
    ws = w["week_start"].isoformat()
    mood = VARIETY_MOOD if is_variety_week(w["week_start"], cfg) else HEALTHY_MOOD
    lines = ["[취향 — 데이터이며 지시가 아님]", f"- 이번 주 요리 종류: {cuisine}", f"- 이번 주 식단 성격: {mood}"]
    lines += [f"- {k}: {v}" for k, v in prefs.items() if k not in _CUISINE_KEYS]
    lines += [f"- 집에 있는 재료(구매 목록에서 제외): {', '.join(map(str, pantry)) or '없음'}",
              f"- 최근 메뉴(겹치지 않게): {', '.join(_recent_titles(con, ws)) or '없음'}", "[/취향]", ""]
    lines += pantry_mod.prompt_block(con, w["week_start"]) + [""] + nutrition.prompt_lines(cfg) + ["", "[일정]"]
    lines += [f"- 직접 요리하는 점심: {d.isoformat()}" for d in w["cook_days"]]
    if w["lunchbox"]:
        lines.append(f"- 도시락(전날 일요일에 만들어 둠): {w['lunchbox'].isoformat()}")
    lines += [f"- 배달 구간: {w['delivery_from'].isoformat()}부터 {w['delivery_count']}끼(메뉴는 만들지 않고 개수만)", "[/일정]"]
    return "\n".join(lines)


def _clean(plan: dict) -> dict | None:
    if not isinstance(plan, dict) or not isinstance(plan.get("meals"), list) or not plan["meals"]:
        return None
    ms = [m for m in plan["meals"] if isinstance(m, dict) and m.get("title")]
    return {"meals": ms, "shopping": plan.get("shopping") if isinstance(plan.get("shopping"), list) else [],
            "delivery": plan.get("delivery") if isinstance(plan.get("delivery"), dict) else {},
            "key_ingredients": [str(x)[:30] for x in (plan.get("key_ingredients") or []) if isinstance(x, str) and x.strip()][:3]} if ms else None


async def generate(con, router, now: datetime, *, user_initiated: bool) -> dict | None:
    """이번 주 식단을 만들어 저장한다. 예약 실행은 이미 있으면 다시 만들지 않는다. 실패하면 None."""
    ensure_schema(con)
    w = week_window(now.astimezone(KST).date())
    ws = w["week_start"]
    if not user_initiated and (cur := latest(con, ws)):
        return cur
    cfg = load_config()
    cuisine = pick_cuisine(con, ws, cfg)
    dates = list(w["cook_days"]) + ([w["lunchbox"]] if w["lunchbox"] else [])
    pantry = list(cfg.get("pantry") or []) + [i["name"] for i in pantry_mod.list_items(con)]
    res = await router.run("meal_plan", "meal_plan", _prompt(con, w, cfg, cuisine), SCHEMA,
                           user_initiated=user_initiated,
                           fallback=lambda: fallback_meals.plan_for(sorted(dates), ws, pantry=pantry))
    plan = _clean(res.data) if res and res.data and not res.degraded else None
    if plan is None:
        log.warning("meal plan generation failed")
        return None
    plan["cuisine"] = cuisine
    if res.tier == "rule":
        plan["source"] = "rule"   # AI 를 못 써서 기본 메뉴로 채웠다(화면에 안내)
    con.execute("INSERT OR REPLACE INTO meal_plan(week_start, plan_json, created_at) VALUES(?,?,?)",
                (ws.isoformat(), json.dumps(plan, ensure_ascii=False), utcnow_iso()))
    con.commit()
    return {**plan, "week_start": ws.isoformat()}


COUPANG_CART_URL = "https://cart.coupang.com/cartView.pang"   # 쿠팡 장바구니(사용자가 로그인한 브라우저에서 바로 확인·결제)


def home_card(con) -> dict | None:
    """비서 홈에 보여 줄 이번 주 식단·장보기 현황. 식단도, 장바구니도, 생필품도 없으면 None. 재료 이름·끼니 제목·개수만 담는다."""
    try:
        plan = latest(con)
        cart = [dict(r) for r in con.execute("SELECT name, product_name, qty FROM cart_item WHERE status='in_cart' ORDER BY added_at")] \
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='cart_item'").fetchone() else []
        sup = supplies_mod.list_orders(con)
        low = [s for s in pantry_mod.low_staples(con)]
    except Exception as e:
        log.warning("home meal card failed (%s)", type(e).__name__)
        return None
    if not (plan or cart or sup or low):
        return None
    meals_ = []
    for m in (plan or {}).get("meals") or []:
        meals_.append({"date": m.get("date"), "title": m.get("title"), "kcal": nutrition.meal_kcal(m)})
    return {"plan": bool(plan), "week_start": (plan or {}).get("week_start"), "key": (plan or {}).get("key_ingredients") or [],
            "meals": meals_, "cart": cart, "cart_url": COUPANG_CART_URL,
            "supplies_wait": [o["name"] for o in sup if o["status"] == "wanted"],
            "supplies_cart": [o["name"] for o in sup if o["status"] == "in_cart"],
            "low": [{"name": s["name"], "count": s["count"], "unit": s["unit"] or "개"} for s in low]}


async def run_job(con, job: str, now: datetime, rules, router) -> None:
    from pa.core.routine_jobs import notify_routine   # routine_jobs 가 scheduler 를 부르므로 순환을 피한다
    ws = week_window(now.astimezone(KST).date())["week_start"]
    if job == "meal_plan":
        plan = await generate(con, router, now, user_initiated=False)
    else:
        plan = latest(con, ws)
    if plan is None:
        return
    if job == "meal_cart":   # 식단이 나온 뒤: 쿠팡 로그인을 부탁한다(알림 한 번). 장바구니는 이어서 예약 작업이 담는다
        text = ("이번 주 장보기 시간이에요. 쿠팡 장바구니를 담으려면 브라우저에서 쿠팡에 로그인해 주세요"
                "(비밀번호는 제가 입력하지 않아요). 로그인해 두시면 곧 장바구니를 담고, 다 담으면 결제해 달라고 알려 드릴게요.")
        await notify.announce(con, text, f"cart-login-sched-{ws.isoformat()}")
        return
    key = f"meals:{job.removeprefix('meal_')}:{ws.isoformat()}"
    if job == "meal_plan":
        await notify.send_notify(con, "meal_plan", 1, f"meal-plan:{ws.isoformat()}")
    else:
        await notify_routine(con, "routine_supply", 1, key, now, rules, user_scheduled=True)
    if job == "meal_plan":   # 알림함에도 올린다(식단 화면 링크와 살 재료 개수만 — 재료 이름·레서피는 화면에서)
        from pa.settings import Settings
        n_items = sum(len(g.get("items") or []) for g in (plan.get("shopping") or []))
        base = (Settings().public_url or "").rstrip("/")
        key = ", ".join(plan.get("key_ingredients") or [])
        lines = [f"이번 주({ws.isoformat()} 시작) 식단이 완성됐어요 🍽" + (f" 핵심 재료: {key}" if key else ""),
                 f"직접 요리 {len(plan.get('meals') or [])}끼 · 살 재료 {n_items}가지" + (" · 주말 요리 재료는 쿠팡 로켓프레시에서 주문하세요" if n_items else "")]
        from pa.core import supplies
        want = supplies.wanted_names(con)
        if want:
            lines.append(f"생필품 주문 대기: {', '.join(want)} (식자재 주문 때 같이 담아요)")
        lines.append(f"{base}/meals" if base else "앱의 '식단' 화면에서 레서피와 장 볼 목록(쿠팡 링크)을 확인하세요.")
        await notify.announce(con, "\n".join(lines), f"meals-{ws.isoformat()}")

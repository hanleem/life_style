"""AI 를 쓸 수 없을 때의 식단 대체 동작. config/fallback_meals.yaml(기본 가정식 4주분)에서 골라 식단 형식으로 만든다.

한 주 = 저녁·아침 매일 + 평일 간식 + (선택) 주말 점심 + 일요일 밑반찬. 알레르기·싫어하는 음식이 든 메뉴는 피하고,
양은 가족 인분 합계에 맞춰 늘리고 줄인다. 각 주는 성장 영양 체크(pa.core.growth)를 통과하도록 짜 두었다.
"""
import functools
import re
from datetime import date
from pathlib import Path

import yaml

from pa.core import household

PATH = Path(__file__).resolve().parent.parent.parent / "config" / "fallback_meals.yaml"
BASE_PORTIONS = 3.35   # yaml 의 분량은 어른 2 + 초등 아이 2(10세·8세)의 인분 합계 기준
_POOLS = ("dinners", "breakfasts", "snacks", "lunches", "prep")


@functools.lru_cache(maxsize=1)
def _weeks() -> list[dict]:
    return (yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}).get("weeks") or []


_NUM = re.compile(r"^\s*(\d+(?:\.\d+)?)(?:/(\d+))?(.*)$")


def scale_qty(qty: str, factor: float) -> str:
    """'600g', '1모', '1/2개' 같은 양에 factor 를 곱한다. 숫자로 읽을 수 없는 양('적당량')은 그대로 둔다."""
    m = _NUM.match(str(qty))
    if not m or abs(factor - 1) < 0.15:
        return str(qty)
    val = float(m.group(1)) / (int(m.group(2)) if m.group(2) else 1)
    unit = m.group(3)
    new = val * factor
    if unit.strip().lower().startswith(("g", "ml")):
        out = round(new / 10) * 10 if new >= 50 else round(new)
    else:
        new = 1.0 if (new < 1 and val >= 1) else max(round(new * 2) / 2, 0.5)
        out = int(new) if new == int(new) else new
    return f"{out:g}{unit}"


def _text(dish: dict) -> str:
    return dish["title"] + " ".join(i["n"] for i in dish["ingredients"])


def _pick(pool: list[dict], others: list[dict], i: int, avoid: list[str], used: set[str]) -> dict:
    """pool[i] 를 쓰되 피할 재료가 있으면 다른 메뉴로 바꾼다. 같은 칸에서 같은 메뉴를 되풀이하지 않는다."""
    ok = lambda d: not any(a and a in _text(d) for a in avoid)
    first = pool[i % len(pool)]
    if ok(first):
        return first
    for d in pool[i % len(pool):] + pool[:i % len(pool)] + others:
        if ok(d) and d["title"] not in used:
            return d
    return first


def plan_for(w: dict, members: list[dict], *, weekend_lunch: bool = False, avoid=(), pantry=()) -> dict:
    """주간 식단(week_window 의 날짜들)을 대체 메뉴로 채운다."""
    weeks = _weeks()
    k = (w["week_start"].toordinal() // 7) % len(weeks)
    cur = weeks[k]
    portions = household.total_portions(members) or BASE_PORTIONS
    factor = portions / BASE_PORTIONS
    avoid = [str(a).strip() for a in avoid if str(a).strip()]
    have = {str(p).strip() for p in pantry}

    def rest(pool: str) -> list[dict]:
        return [d for j in range(1, len(weeks)) for d in weeks[(k + j) % len(weeks)][pool]]

    slots: list[tuple[str, str, list[date]]] = [
        ("breakfast", "breakfasts", list(w["days"])), ("dinner", "dinners", list(w["days"])),
        ("snack", "snacks", list(w["school_days"])), ("lunch", "lunches", list(w["weekend_days"]) if weekend_lunch else []),
        ("prep", "prep", [w["prep_day"]])]
    out, cart = [], {}
    for slot, pool, days in slots:
        used: set[str] = set()
        for i, d in enumerate(days):
            dish = _pick(cur[pool], rest(pool), i, avoid, used)
            used.add(dish["title"])
            ings = [{"name": x["n"], "qty": scale_qty(str(x["q"]), factor)} for x in dish["ingredients"]]
            out.append({"date": d.isoformat(), "slot": slot, "title": dish["title"], "why_new": "",
                        "time_min": int(dish["time_min"]), "servings": portions, "ingredients": ings,
                        "steps": list(dish["steps"]), "veg_sides": list(dish.get("sides") or []), "kid_spice": 0,
                        "adult_note": str(dish.get("adult_note") or "")})
            for x, scaled in zip(dish["ingredients"], ings):
                if x["n"] in have:
                    continue
                grp = cart.setdefault(x["c"], {})
                grp[x["n"]] = f"{grp[x['n']]} + {scaled['qty']}" if x["n"] in grp else scaled["qty"]
    shopping = [{"category": c, "items": [{"name": n, "qty": q} for n, q in items.items()]} for c, items in cart.items()]
    return {"meals": out, "shopping": shopping, "delivery": {}, "key_ingredients": [], "source": "rule"}

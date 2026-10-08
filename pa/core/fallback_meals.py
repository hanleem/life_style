"""AI 를 쓸 수 없을 때의 식단 대체 동작. config/fallback_meals.yaml(기본 가정식 4주분)에서 골라 식단 형식으로 만든다."""
import functools
from datetime import date
from pathlib import Path

import yaml

PATH = Path(__file__).resolve().parent.parent.parent / "config" / "fallback_meals.yaml"


@functools.lru_cache(maxsize=1)
def _weeks() -> list[list[dict]]:
    data = yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}
    return [list(w.get("dinners") or []) for w in data.get("weeks") or []]


def _merge_qty(a: str, b: str) -> str:
    return a if a == b else f"{a} + {b}"


def plan_for(dates: list[date], week_start: date, *, servings: int = 4, pantry=()) -> dict:
    """dates 의 날짜마다 저녁 한 끼를 배정한다. 같은 주는 항상 같은 메뉴고, 주가 바뀌면 4주 순환으로 달라진다."""
    weeks = _weeks()
    dinners = weeks[(week_start.toordinal() // 7) % len(weeks)]
    have = {str(p).strip() for p in pantry}
    meals_, cart = [], {}
    for i, d in enumerate(dates):
        dish = dinners[i % len(dinners)]
        ings = [{"name": x["n"], "qty": str(x["q"])} for x in dish["ingredients"]]
        meals_.append({"date": d.isoformat(), "slot": "cook", "title": dish["title"], "why_new": "",
                       "time_min": int(dish["time_min"]), "servings": servings, "ingredients": ings,
                       "steps": list(dish["steps"])})
        for x in dish["ingredients"]:
            if x["n"] in have:
                continue
            cat = cart.setdefault(x["c"], {})
            cat[x["n"]] = _merge_qty(cat[x["n"]], str(x["q"])) if x["n"] in cat else str(x["q"])
    shopping = [{"category": c, "items": [{"name": n, "qty": q} for n, q in items.items()]} for c, items in cart.items()]
    return {"meals": meals_, "shopping": shopping, "delivery": {}, "key_ingredients": []}

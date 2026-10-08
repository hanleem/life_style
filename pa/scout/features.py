"""Feature extraction for taste learning (DESIGN §16.2)."""
from __future__ import annotations

import re

U = "unknown"


def _s(v) -> str:
    if v is None:
        return U
    v = str(v).strip()
    return v or U


def duration_bucket(sec: int | None) -> str:
    if sec is None:
        return U
    if sec <= 300:
        return "≤5"
    if sec <= 600:
        return "5-10"
    if sec <= 900:
        return "10-15"
    return "15+"


def oneway_bucket(min_est: list[int] | None) -> str:
    """Roundtrip estimate upper bound / 2, in minutes."""
    if not min_est:
        return U
    one = max(min_est) / 2
    if one <= 30:
        return "≤30"
    if one <= 60:
        return "30-60"
    if one <= 90:
        return "60-90"
    if one <= 120:
        return "90-120"
    return "120+"


def price_bucket(won: int | None) -> str:
    if won is None:
        return U
    if won <= 15000:
        return "≤15k"
    if won <= 30000:
        return "15-30k"
    return "30k+"


def _walk_minutes_bucket(m) -> str:
    if not isinstance(m, (int, float)):
        return U
    if m <= 30:
        return "≤30"
    if m <= 60:
        return "30-60"
    return "60+"



def _price_won(price) -> int | None:
    if isinstance(price, dict):
        price = price.get("value")
    if isinstance(price, (int, float)):
        return int(price)
    if isinstance(price, str):
        digits = re.sub(r"[^\d]", "", price)
        return int(digits) if digits else None
    return None


def outing_features(course: dict) -> dict[str, str]:
    drive = course.get("drive") or {}
    walk = course.get("walk") or {}
    stops = drive.get("stops") or []
    return {
        "region": _s(stops[0]) if stops else _s(drive.get("region")),
        "oneway_bucket": oneway_bucket(drive.get("roundtrip_min_est")),
        "walk_type": _s(walk.get("type")),
        "walk_minutes_bucket": _walk_minutes_bucket(walk.get("minutes")),
        "crowd_level": _s(course.get("crowd_level") or walk.get("crowd_level")),
    }


def food_features(course: dict) -> dict[str, str]:
    food = course.get("food") or {}
    light = str(food.get("light_menu") or "").lower()
    return {
        "place": _s(food.get("place")),
        "cuisine": _s(food.get("cuisine")),
        "novelty": _s(food.get("novelty")),
        "price_bucket": price_bucket(_price_won(food.get("price"))),
        "takeout": _s(food.get("takeout")),
        "light_menu": light if light in ("yes", "no") else U,
    }


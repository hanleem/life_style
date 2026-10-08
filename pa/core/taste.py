"""Explainable taste learning (DESIGN §16.3)."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable
from datetime import datetime, timedelta, timezone

from pa.core import recs
from pa.ids import new_id

SCORE = {"like": 1.0, "soso": 0.1, "dislike": -1.0}
REASON_PENALTY = -0.5
DECAY = 0.97
EXPLORATION_BONUS = 0.3
EXCLUDE_DAYS = 90
EXCLUDE_KEYS = ("creator", "show", "place")
BONUS_KEYS = ("creator", "region")
NOVELTY_KEYS = ("creator", "region", "show")
PINS_KEY = "taste_pins"
PIN_MARK = "\U0001F4CC"

REASON_FEATURE: dict[str, str] = {
    "너무 멂": "oneway_bucket", "붐빔": "crowd_level", "음식 별로": "cuisine", "비쌈": "price_bucket",
}


def _reason_feature(reason: str, domain: str) -> str | None:
    return REASON_FEATURE.get(reason)


def _contribution(features: dict, rating: str, reasons: list[str], domain: str) -> dict[tuple[str, str], float]:
    """Per-(feature, value) delta for one rating, before decay."""
    out: dict[tuple[str, str], float] = {}
    for f, v in features.items():
        out[(f, str(v))] = out.get((f, str(v)), 0.0) + SCORE[rating]
    for r in reasons:
        f = _reason_feature(r, domain)
        if f is not None and f in features:
            out[(f, str(features[f]))] += REASON_PENALTY
    return out


def _weeks(prev: str | None, now: datetime) -> int:
    if not prev:
        return 0
    try:
        p = datetime.fromisoformat(prev)
    except ValueError:
        return 0
    if p.tzinfo is None:
        p = p.replace(tzinfo=timezone.utc)
    return max(0, (now - p).days // 7)


def apply_feedback(con: sqlite3.Connection, rec_id: str, rating: str, reasons: list[str],
                   note: str | None, now: datetime) -> None:
    if rating not in SCORE:
        raise ValueError(rating)
    rec = recs.get_recommendation(con, rec_id)
    domain = rec["domain"]
    features = json.loads(rec["features_json"])
    reasons = recs.validate_reasons(domain, reasons)
    con.execute("BEGIN IMMEDIATE")
    try:
        prev = recs.last_rating(con, rec_id)
        delta: dict[tuple[str, str], float] = {}
        if prev is not None:
            for k, d in _contribution(features, prev[0], prev[1], domain).items():
                delta[k] = delta.get(k, 0.0) - d
        for k, d in _contribution(features, rating, reasons, domain).items():
            delta[k] = delta.get(k, 0.0) + d
        # inline insert (recs.insert_feedback commits; we need a single transaction)
        con.execute(
            "INSERT INTO feedback (id, recommendation_id, rating, reasons_json, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (new_id(), rec_id, rating, json.dumps(reasons, ensure_ascii=False), note, now.isoformat()))
        n_inc = 1 if prev is None else 0
        for (f, v), d in delta.items():
            row = con.execute("SELECT weight, n, updated_at FROM taste_weight WHERE domain=? AND feature=? AND value=?",
                              (domain, f, v)).fetchone()
            if row is None:
                con.execute("INSERT INTO taste_weight (domain, feature, value, weight, n, updated_at) VALUES (?,?,?,?,?,?)",
                            (domain, f, v, d, n_inc, now.isoformat()))
            else:
                w = row["weight"] * DECAY ** _weeks(row["updated_at"], now) + d
                con.execute("UPDATE taste_weight SET weight=?, n=?, updated_at=? WHERE domain=? AND feature=? AND value=?",
                            (w, row["n"] + n_inc, now.isoformat(), domain, f, v))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


# ---- pins -------------------------------------------------------------

def _pins(con) -> set[tuple[str, str, str]]:
    row = con.execute("SELECT value FROM preference WHERE key=?", (PINS_KEY,)).fetchone()
    if row is None:
        return set()
    return {tuple(x) for x in json.loads(row["value"])}


def _save_pins(con, pins: set[tuple[str, str, str]], now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    con.execute(
        "INSERT INTO preference (key, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (PINS_KEY, json.dumps(sorted(pins), ensure_ascii=False), now.isoformat()))


def _effective(weight: float, pinned: bool) -> float:
    if pinned and abs(weight) < 1.0:
        return -1.0 if weight < 0 else 1.0
    return weight


def pin_weight(con, domain: str, feature: str, value: str, pinned: bool = True,
               now: datetime | None = None) -> None:
    pins = _pins(con)
    (pins.add if pinned else pins.discard)((domain, feature, value))
    _save_pins(con, pins, now)


def list_weights(con, domain: str | None = None) -> list[sqlite3.Row]:
    if domain is None:
        return con.execute("SELECT * FROM taste_weight ORDER BY domain, weight DESC, feature, value").fetchall()
    return con.execute("SELECT * FROM taste_weight WHERE domain=? ORDER BY weight DESC, feature, value",
                       (domain,)).fetchall()


def delete_weight(con, domain: str, feature: str, value: str) -> None:
    con.execute("DELETE FROM taste_weight WHERE domain=? AND feature=? AND value=?", (domain, feature, value))
    pins = _pins(con)
    if (domain, feature, value) in pins:
        pins.discard((domain, feature, value))
        _save_pins(con, pins)


def reset(con, domain: str | None = None) -> None:
    if domain is None:
        con.execute("DELETE FROM taste_weight")
        _save_pins(con, set())
    else:
        con.execute("DELETE FROM taste_weight WHERE domain=?", (domain,))
        _save_pins(con, {p for p in _pins(con) if p[0] != domain})


# ---- scoring ----------------------------------------------------------

def score_candidate(con, domain: str, features: dict[str, str], now: datetime) -> float:
    pins = _pins(con)
    total = 0.0
    for f, v in features.items():
        row = con.execute("SELECT weight, n FROM taste_weight WHERE domain=? AND feature=? AND value=?",
                          (domain, f, str(v))).fetchone()
        if row is None:
            continue
        w = _effective(row["weight"], (domain, f, str(v)) in pins)
        total += w / math.sqrt(1 + row["n"])
    return total


def excluded_refs(con, domain: str, now: datetime) -> set[tuple[str, str]]:
    cutoff = now - timedelta(days=EXCLUDE_DAYS)
    counts: dict[tuple[str, str], int] = {}
    for r in con.execute("SELECT id, features_json FROM recommendation WHERE domain=?", (domain,)).fetchall():
        row = con.execute("SELECT rating, created_at FROM feedback WHERE recommendation_id=?"
                          " ORDER BY created_at DESC, id DESC LIMIT 1", (r["id"],)).fetchone()
        if row is None or row["rating"] != "dislike":
            continue
        ts = datetime.fromisoformat(row["created_at"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts <= cutoff:
            continue
        feats = json.loads(r["features_json"])
        for k in EXCLUDE_KEYS:
            if k in feats:
                key = (k, str(feats[k]))
                counts[key] = counts.get(key, 0) + 1
    return {k for k, c in counts.items() if c >= 2}


@dataclass
class Candidate:
    key: str
    features: dict[str, str]
    payload: dict = field(default_factory=dict)


def rank_with_exploration(con, domain: str, candidates: list[Candidate], now: datetime, k: int = 3,
                          recent_keys: set[tuple[str, str]] = frozenset(),
                          bonus: Callable[[Candidate], float] | None = None) -> list[tuple[Candidate, bool]]:
    excl = excluded_refs(con, domain, now)
    pool = [c for c in candidates if not any((f, str(v)) in excl for f, v in c.features.items())]

    def score(c: Candidate) -> float:
        s = score_candidate(con, domain, c.features, now)
        for f in BONUS_KEYS:
            if f in c.features and (f, str(c.features[f])) not in recent_keys:
                s += EXPLORATION_BONUS
        if bonus is not None:
            s += bonus(c)
        return s

    scored = sorted(((score(c), i, c) for i, c in enumerate(pool)), key=lambda t: (-t[0], t[1]))
    ordered = [c for _, _, c in scored]
    if k <= 0 or not ordered:
        return []
    if len(ordered) <= 1 or k == 1:
        return [(c, False) for c in ordered[:k]]
    top, rest = ordered[:k - 1], ordered[k - 1:]
    if not rest:
        return [(c, False) for c in top]

    def novelty(c: Candidate) -> int:
        n = 0
        for f in NOVELTY_KEYS:
            if f not in c.features or (f, str(c.features[f])) in recent_keys:
                continue
            if con.execute("SELECT 1 FROM taste_weight WHERE domain=? AND feature=? AND value=?",
                           (domain, f, str(c.features[f]))).fetchone() is None:
                n += 1
        return n

    best, best_n = None, 0
    for c in rest:
        nv = novelty(c)
        if nv > best_n:
            best, best_n = c, nv
    if best is None:
        return [(c, False) for c in top] + [(rest[0], False)]
    return [(c, False) for c in top] + [(best, True)]


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _clean(v, n: int = 40) -> str:
    return re.sub(r"\s+", " ", _CTRL.sub(" ", str(v))).strip()[:n]


def taste_profile_text(con, domain: str, limit: int = 5) -> str:
    pins = _pins(con)
    rows = []
    for r in con.execute("SELECT feature, value, weight FROM taste_weight WHERE domain=?", (domain,)):
        pinned = (domain, r["feature"], r["value"]) in pins
        rows.append((r["value"], _effective(r["weight"], pinned), pinned))
    likes = sorted([r for r in rows if r[1] > 0], key=lambda r: -r[1])[:limit]
    dislikes = sorted([r for r in rows if r[1] < 0], key=lambda r: r[1])[:limit]
    if not likes and not dislikes:
        return "아직 학습된 취향 없음"

    def fmt(r):
        return (PIN_MARK if r[2] else "") + _clean(r[0])

    return f"선호: {', '.join(map(fmt, likes))} / 비선호: {', '.join(map(fmt, dislikes))}"

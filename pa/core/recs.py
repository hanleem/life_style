"""Recommendation / feedback / taste store (DESIGN §16.1, preference §3.1)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Literal

from pa.core.errors import NotFound
from pa.ids import new_id

Domain = Literal["outing", "food"]

REASON_CHIPS: dict[str, tuple[str, ...]] = {
    "outing": ("너무 멂", "붐빔", "음식 별로", "비쌈"),
}
_GROUP = {"outing": "outing", "food": "outing"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS preference (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS recommendation (
  id TEXT PRIMARY KEY, domain TEXT CHECK (domain IN ('outing','food')),
  item_ref TEXT NOT NULL,
  features_json TEXT NOT NULL,
  query_used TEXT, sent_at TEXT NOT NULL, opened_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL REFERENCES recommendation(id),
  rating TEXT NOT NULL CHECK (rating IN ('like','soso','dislike')),
  reasons_json TEXT, note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS taste_weight (
  domain TEXT, feature TEXT, value TEXT, weight REAL NOT NULL DEFAULT 0, n INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT, PRIMARY KEY (domain, feature, value)
);
CREATE INDEX IF NOT EXISTS ix_feedback_rec ON feedback(recommendation_id, created_at);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)
    con.commit()


def validate_reasons(domain: str, reasons: list[str]) -> list[str]:
    allowed = REASON_CHIPS.get(_GROUP.get(domain, ""), ())
    out: list[str] = []
    for r in reasons:
        if r in allowed and r not in out:
            out.append(r)
    return out


def record_recommendation(con, *, domain: str, item_ref: str, features: dict[str, str],
                          query_used: str | None, now: datetime) -> str:
    rid = new_id()
    con.execute(
        "INSERT INTO recommendation (id, domain, item_ref, features_json, query_used, sent_at)"
        " VALUES (?,?,?,?,?,?)",
        (rid, domain, item_ref, json.dumps(features, ensure_ascii=False), query_used, now.isoformat()))
    con.commit()
    return rid


def mark_opened(con, rec_id: str, now: datetime) -> None:
    con.execute("UPDATE recommendation SET opened_at=? WHERE id=? AND opened_at IS NULL",
                (now.isoformat(), rec_id))
    con.commit()


def get_recommendation(con, rec_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM recommendation WHERE id=?", (rec_id,)).fetchone()
    if row is None:
        raise NotFound(rec_id)
    return row


def insert_feedback(con, rec_id: str, rating: str, reasons: list[str], note: str | None,
                    now: datetime) -> str:
    fid = new_id()
    con.execute(
        "INSERT INTO feedback (id, recommendation_id, rating, reasons_json, note, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (fid, rec_id, rating, json.dumps(reasons, ensure_ascii=False), note, now.isoformat()))
    con.commit()
    return fid


def last_rating(con, rec_id: str) -> tuple[str, list[str]] | None:
    row = con.execute(
        "SELECT rating, reasons_json FROM feedback WHERE recommendation_id=?"
        " ORDER BY created_at DESC, id DESC LIMIT 1", (rec_id,)).fetchone()
    if row is None:
        return None
    return row["rating"], json.loads(row["reasons_json"] or "[]")

"""Scout store: 나들이 추천 표."""
from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outing_suggestion (
  id TEXT PRIMARY KEY, for_week TEXT, course_json TEXT NOT NULL,
  sources_json TEXT NOT NULL, status TEXT CHECK (status IN ('proposed','chosen','skipped','done')),
  feedback TEXT, created_at TEXT NOT NULL
);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)

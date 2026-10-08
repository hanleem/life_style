import sqlite3

import pytest

from pa.db import SCHEMA_VERSION, connect, get_cursor, init_db, set_cursor
from pa.ids import new_id


def test_init_db_creates_tables(tmp_path):
    con = connect(tmp_path / "core.db")
    init_db(con)
    names = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"draft", "draft_version", "approval", "action", "audit_log",
            "llm_call", "llm_cache", "auth_nonce", "task", "task_source"} <= names
    assert not {n for n in names if n.startswith(("mail_", "chat_", "lab_"))}
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_db_creates_routine_tables(tmp_path):
    con = connect(tmp_path / "core.db")
    init_db(con)
    names = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"supply_item", "supply_stock", "supply_event", "supply_order",
            "supply_rotation", "routine_plan", "routine_proposal"} <= names


def test_init_db_is_idempotent(tmp_path):
    con = connect(tmp_path / "core.db")
    init_db(con)
    init_db(con)


def test_new_id_is_sortable_unique():
    a, b = new_id(), new_id()
    assert a != b and len(a) == 26


def test_cursor_roundtrip(con):
    assert get_cursor(con, "calendar:holidays") is None
    set_cursor(con, "calendar:holidays", "42")
    set_cursor(con, "calendar:holidays", "43")
    assert get_cursor(con, "calendar:holidays") == "43"
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_task_source_types_are_limited(con):
    now = "2026-10-06T00:00:00Z"
    con.execute("INSERT INTO task(id,title,bucket,created_at,updated_at) VALUES('t1','x','backlog',?,?)", (now, now))
    con.execute("INSERT INTO task_source(id,task_id,source_type,source_ref,observed_at) VALUES('s1','t1','calendar_event','a',?)", (now,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO task_source(id,task_id,source_type,source_ref,observed_at) VALUES('s2','t1','bogus','b',?)", (now,))

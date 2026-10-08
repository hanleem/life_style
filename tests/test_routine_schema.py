import sqlite3

import pytest

from pa.db import SCHEMA_VERSION, connect, init_db

NEW_TABLES = ["supply_item", "supply_stock", "supply_event", "supply_order",
              "supply_rotation", "routine_plan", "routine_proposal"]
NOW = "2026-10-06T00:00:00Z"
Q = ("SELECT name, sql FROM sqlite_master WHERE name LIKE 'supply%' OR name LIKE 'routine%' "
     "OR name LIKE 'ux_supply%' OR name LIKE 'ix_supply%' OR name LIKE 'ix_routine%' ORDER BY name")


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "core.db")
    init_db(c)
    return c


def _item(con, id="i1"):
    con.execute("INSERT INTO supply_item(id,label,meals_per_workday,created_at) VALUES(?,?,?,?)",
                (id, "샐러드 정기배송", 2, NOW))


def test_fresh_and_migrated_v10_have_identical_routine_schema(tmp_path):
    fresh = connect(tmp_path / "a.db")
    init_db(fresh)
    old = connect(tmp_path / "b.db")
    init_db(old)
    for t in reversed(NEW_TABLES):  # 인덱스는 테이블과 함께 사라진다
        old.execute(f"DROP TABLE {t}")
    old.execute("PRAGMA user_version=10")
    old.execute("INSERT INTO task(id,title,bucket,created_at,updated_at) VALUES('t1','x','backlog',?,?)", (NOW, NOW))
    init_db(old)
    assert fresh.execute(Q).fetchall() == old.execute(Q).fetchall()
    assert len(fresh.execute(Q).fetchall()) >= len(NEW_TABLES) + 2
    assert old.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert old.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 1


def test_init_db_idempotent_keeps_routine_rows(con):
    _item(con)
    init_db(con)
    init_db(con)
    assert con.execute("SELECT COUNT(*) FROM supply_item").fetchone()[0] == 1


def test_fk_enforced(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO supply_stock(item_id,remaining,updated_at) VALUES('nope',1,?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO supply_event(id,item_id,kind,qty,for_date,created_at) VALUES('e','nope','delivered',1,'2026-10-06',?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO supply_order(id,item_id,ordered_at) VALUES('o','nope',?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO supply_rotation(id,item_id,label,verified_at) VALUES('r','nope','x',?)", (NOW,))


def test_remaining_never_negative(con):
    _item(con)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO supply_stock(item_id,remaining,updated_at) VALUES('i1',-1,?)", (NOW,))
    con.execute("INSERT INTO supply_stock(item_id,remaining,updated_at) VALUES('i1',0,?)", (NOW,))


def test_meals_check_ranges(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO supply_item(id,label,meals_per_workday,created_at) VALUES('a','x',0,?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO supply_item(id,label,meals_per_workday,created_at) VALUES('a','x',7,?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO supply_item(id,label,meals_per_workday,meals_per_weekend_day,created_at) VALUES('a','x',1,7,?)", (NOW,))


def _ev(con, id, kind, d="2026-10-06"):
    con.execute("INSERT INTO supply_event(id,item_id,kind,qty,for_date,created_at) VALUES(?,?,?,?,?,?)",
                (id, "i1", kind, 1, d, NOW))


def test_consumed_once_per_day(con):
    _item(con)
    _ev(con, "e1", "consumed")
    with pytest.raises(sqlite3.IntegrityError):
        _ev(con, "e2", "consumed")
    _ev(con, "e3", "consumed", "2026-10-07")
    _ev(con, "d1", "delivered")
    _ev(con, "d2", "delivered")


def test_skipped_once_per_day_and_kind_check(con):
    _item(con)
    _ev(con, "s1", "skipped")
    with pytest.raises(sqlite3.IntegrityError):
        _ev(con, "s2", "skipped")
    with pytest.raises(sqlite3.IntegrityError):
        _ev(con, "x", "bogus")


def test_routine_plan_source_check(con):
    ins = "INSERT INTO routine_plan(key,value_json,source,effective_from,updated_at) VALUES(?,?,?,?,?)"
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, ("k", "1", "coach", "2026-10-06", NOW))
    con.execute(ins, ("k", "1", "user", "2026-10-06", NOW))
    con.execute(ins, ("k2", "1", "coach_accepted", "2026-10-06", NOW))


def test_routine_proposal_response_check(con):
    ins = "INSERT INTO routine_proposal(id,key,value_json,basis,shown_at,response) VALUES(?,?,?,?,?,?)"
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, ("p", "k", "1", "b", NOW, "ignored"))
    con.execute(ins, ("p", "k", "1", "b", NOW, "accepted"))
    con.execute(ins, ("p2", "k", "1", "b", NOW, None))

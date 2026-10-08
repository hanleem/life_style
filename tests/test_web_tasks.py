"""할 일 화면과 API — 전부 소유자 전용."""
import pytest
from conftest import owner_client
from fastapi.testclient import TestClient

from pa.settings import Settings
from pa.web.app import create_app


def _settings():
    return Settings(session_secret="t" * 32, dev_skip_tailscale=True)


@pytest.fixture
def web(con):
    return owner_client(create_app(_settings(), con, router=None))


def _mk(con, tid, status="open", src=None):
    con.execute("INSERT INTO task(id,title,bucket,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)", (tid, "일", "check_soon", status, "2026-10-06T00:00:00+00:00", "2026-10-06T00:00:00+00:00"))
    if src:
        con.execute("INSERT INTO task_source(id,task_id,source_type,source_ref,observed_at) VALUES(?,?,?,?,?)",
                    (f"s{tid}", tid, "memo", src, "2026-10-06T00:00:00+00:00"))


def test_add_and_list_tasks(web, con):
    r = web.post("/api/tasks", json={"title": "학교 준비물 챙기기"})
    assert r.status_code == 200 and r.json()["ok"]
    assert "학교 준비물 챙기기" in web.get("/tasks").text
    assert web.post("/api/tasks", json={"title": ""}).status_code == 422


def test_complete_and_confirm(web, con):
    _mk(con, "c1", status="candidate")
    _mk(con, "o1")
    assert web.post("/api/tasks/c1/confirm").json() == {"ok": True}
    assert con.execute("SELECT status FROM task WHERE id='c1'").fetchone()[0] == "open"
    assert web.post("/api/tasks/o1/complete").json() == {"ok": True}
    assert con.execute("SELECT status FROM task WHERE id='o1'").fetchone()[0] == "done"
    assert web.post("/api/tasks/nope/complete").status_code == 404


def test_drop_route_one_tap(web, con):
    _mk(con, "t1", src="7:1")
    assert web.post("/api/tasks/t1/drop").json() == {"ok": True}
    assert con.execute("SELECT status FROM task WHERE id='t1'").fetchone()[0] == "dropped"
    assert con.execute("SELECT COUNT(*) FROM task_source").fetchone()[0] == 1
    assert web.post("/api/tasks/t1/restore").json() == {"ok": True}
    assert con.execute("SELECT status FROM task WHERE id='t1'").fetchone()[0] == "candidate"


def test_drop_route_errors(web, con):
    _mk(con, "t1", status="done")
    assert web.post("/api/tasks/nope/drop").status_code == 404
    assert web.post("/api/tasks/t1/drop").status_code == 409
    assert web.post("/api/tasks/t1/restore").status_code == 409


def test_merge_route_404_and_409(web, con):
    _mk(con, "a")
    _mk(con, "b", src="7:2")
    assert web.post("/api/tasks/a/merge", json={"other_id": "zz"}).status_code == 404
    assert web.post("/api/tasks/a/merge", json={"other_id": "a"}).status_code == 409
    assert web.post("/api/tasks/a/merge", json={"other_id": "b"}).json() == {"ok": True}
    assert con.execute("SELECT task_id FROM task_source").fetchone()[0] == "a"


def test_task_routes_require_owner(con):
    c = TestClient(create_app(_settings(), con, router=None))   # 루프백이 아닌 접속
    _mk(con, "t1")
    assert c.get("/tasks").status_code == 401
    assert c.post("/api/tasks/t1/drop").status_code == 401
    assert c.post("/api/tasks/t1/restore").status_code == 401
    assert c.post("/api/tasks/t1/merge", json={"other_id": "x"}).status_code == 401
    assert con.execute("SELECT status FROM task WHERE id='t1'").fetchone()[0] == "open"

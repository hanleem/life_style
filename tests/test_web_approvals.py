from conftest import calendar_draft, owner_client
import pytest
from fastapi.testclient import TestClient

from pa.settings import Settings
from pa.web.app import create_app


@pytest.fixture
def c(con):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True)
    return owner_client(create_app(s, con, None))


def _nonce(c):
    return c.post("/api/nonce").json()["nonce"]


def test_requires_login(con):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True)
    c = TestClient(create_app(s, con, None))
    assert c.get("/api/drafts").status_code == 401


def test_approvals_page_renders_calendar_draft(c, con):
    calendar_draft(con, title="가족 모임")
    r = c.get("/approvals")
    assert r.status_code == 200 and "승인" in r.text
    assert "일정 추가: 가족 모임" in r.text and "승인하고 캘린더에 추가" in r.text
    items = c.get("/api/drafts").json()
    assert len(items) == 1 and items[0]["kind"] == "calendar_event"


def test_edit_route_is_gone(c, con):
    calendar_draft(con)
    did = c.get("/api/drafts").json()[0]["id"]
    assert c.post(f"/api/drafts/{did}/edit", json={"body": "x"}).status_code in (404, 405)


def test_stale_button_returns_409(c, con):
    calendar_draft(con)
    d = c.get("/api/drafts").json()[0]
    r = c.post(f"/api/drafts/{d['id']}/approve", json={"version": 9, "hash": d["hash"], "nonce": _nonce(c)})
    assert r.status_code == 409


def test_approve_without_valid_nonce_forbidden(c, con):
    calendar_draft(con)
    d = c.get("/api/drafts").json()[0]
    r = c.post(f"/api/drafts/{d['id']}/approve", json={"version": d["version"], "hash": d["hash"], "nonce": "bad"})
    assert r.status_code == 403


def test_discard_removes_from_pending(c, con):
    calendar_draft(con)
    d = c.get("/api/drafts").json()[0]
    assert c.post(f"/api/drafts/{d['id']}/discard").status_code == 200
    assert c.get("/api/drafts").json() == []

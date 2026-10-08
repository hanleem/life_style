import json
from datetime import datetime, timezone

import pytest
from conftest import owner_client
from fastapi.testclient import TestClient

from pa.core import recs
from pa.scout import store
from pa.settings import Settings
from pa.web.app import create_app

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)
URL = "https://example.com/place/1"


class BoomRouter:
    calls = 0

    async def run(self, *a, **k):
        BoomRouter.calls += 1
        raise AssertionError("router must not be called")


@pytest.fixture
def ctx(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True,
                 scout_config_path=tmp_path / "none.yaml")
    app = create_app(s, con, BoomRouter())
    return owner_client(app), con


def _outing_rec(con, url=URL, walk="숲길 <b>x</b>"):
    store.ensure_schema(con)
    course = {"walk": {"name": walk}, "food": {"place": "식당A"}, "order": "walk_then_takeout"}
    con.execute("INSERT INTO outing_suggestion (id, for_week, course_json, sources_json, status, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (f"s-{url[-1]}", "2026-W41", json.dumps(course, ensure_ascii=False),
                 json.dumps([{"url": url, "verify_status": "page_ok", "kind": "official"}]), "proposed", NOW.isoformat()))
    sid = f"s-{url[-1]}"
    rid = recs.record_recommendation(con, domain="outing", item_ref=sid,
                                     features={"region": "양평", "crowd_level": "low"}, query_used="q", now=NOW)
    recs.record_recommendation(con, domain="food", item_ref=sid, features={"cuisine": "한식"}, query_used="q", now=NOW)
    con.commit()
    return rid, sid


def _weight(con, domain, feature):
    return con.execute("SELECT weight FROM taste_weight WHERE domain=? AND feature=?", (domain, feature)).fetchone()


def test_outing_page_makes_zero_router_calls(ctx):
    c, con = ctx
    _outing_rec(con)
    BoomRouter.calls = 0
    r = c.get("/outing")
    assert r.status_code == 200 and "숲길" in r.text
    assert "<b>x</b>" not in r.text          # autoescaped
    assert BoomRouter.calls == 0


def test_like_then_dislike_overwrites(ctx):
    c, con = ctx
    rid, _ = _outing_rec(con)
    assert c.post("/api/feedback", json={"rec_id": rid, "rating": "like"}).status_code == 200
    assert _weight(con, "outing", "region")["weight"] == 1.0
    assert c.post("/api/feedback", json={"rec_id": rid, "rating": "dislike"}).status_code == 200
    assert _weight(con, "outing", "region")["weight"] == -1.0


def test_feedback_validation_and_unknown(ctx):
    c, con = ctx
    rid, _ = _outing_rec(con)
    assert c.post("/api/feedback", json={"rec_id": rid, "rating": "love"}).status_code == 422
    assert c.post("/api/feedback", json={"rec_id": "nope", "rating": "like"}).status_code == 404


def test_feedback_filters_bad_reason(ctx):
    c, con = ctx
    rid, _ = _outing_rec(con)
    assert c.post("/api/feedback", json={"rec_id": rid, "rating": "dislike", "reasons": ["해킹", "너무 멂"]}).status_code == 200
    assert con.execute("SELECT reasons_json FROM feedback WHERE recommendation_id=?", (rid,)).fetchone()[0] == json.dumps(["너무 멂"], ensure_ascii=False)
    rid2, _ = _outing_rec(con, "https://example.com/place/2")
    c.post("/api/feedback", json={"rec_id": rid2, "rating": "soso", "reasons": ["해킹"]})
    assert con.execute("SELECT reasons_json FROM feedback WHERE recommendation_id=?", (rid2,)).fetchone()[0] == "[]"


def test_open_records_without_weight_change(ctx):
    c, con = ctx
    rid, _ = _outing_rec(con)
    r = c.post(f"/api/recs/{rid}/open")
    assert r.status_code == 200 and r.json()["url"] == URL
    assert con.execute("SELECT opened_at FROM recommendation WHERE id=?", (rid,)).fetchone()[0]
    assert con.execute("SELECT COUNT(*) FROM taste_weight").fetchone()[0] == 0


def test_run_outing_disabled(ctx):
    c, _ = ctx
    r = c.post("/api/scout/run/outing")
    assert r.status_code == 200
    j = r.json()
    assert j["reason"] == "disabled" and j["degraded"] is True and "출발 지역을 먼저 설정하세요" in j["message"]


def test_settings_scout_rejects_address(ctx):
    c, _ = ctx
    r = c.post("/settings/scout", json={"from_region": "서울 종로구 한빛로 999 ○○아파트"})
    assert r.status_code == 422 and "동 단위까지만 입력하세요" in r.text
    assert c.post("/settings/scout", json={"from_region": "서울 종로구 종로3가"}).status_code == 200
    assert c.post("/settings/scout", json={"from_region": "OO마을"}).status_code == 422
    assert c.post("/settings/scout", json={"from_region": "한빛로 12"}).status_code == 422


def test_taste_reset_domain(ctx):
    c, con = ctx
    rid, _ = _outing_rec(con)
    c.post("/api/feedback", json={"rec_id": rid, "rating": "like"})
    assert con.execute("SELECT COUNT(*) FROM taste_weight WHERE domain='outing'").fetchone()[0] > 0
    assert c.post("/api/taste/reset", json={"domain": "outing"}).status_code == 200
    assert con.execute("SELECT COUNT(*) FROM taste_weight WHERE domain='outing'").fetchone()[0] == 0
    assert c.get("/settings/taste").status_code == 200
    assert c.post("/api/taste/reset", json={"domain": "meditation"}).status_code == 422


def test_requires_login(con):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True)
    c = TestClient(create_app(s, con, BoomRouter()))   # 루프백이 아닌 접속
    assert c.get("/outing").status_code == 401
    assert c.post("/api/feedback", json={"rec_id": "x", "rating": "like"}).status_code == 401
    assert c.get("/settings/taste").status_code == 401
    assert c.get("/settings/scout").status_code == 401

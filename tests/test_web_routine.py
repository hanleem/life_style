"""R7: /routine 화면, 설정, 공급 API, 삭제·내보내기."""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import owner_client
from fastapi.testclient import TestClient

from pa.core import coach_copy, routine_plan, supply
from pa.db import connect, init_db
from pa.settings import Settings
from pa.web import routes_routine
from pa.web.app import create_app

UTC = timezone.utc
T = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)   # 화 21:00 KST
TODAY = "2026-10-06"
STATIC = Path(routes_routine.__file__).parent / "static"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    con = connect(tmp_path / "pa.db")
    init_db(con)
    monkeypatch.setattr(routes_routine, "_now", lambda: T)
    return con, None, tmp_path


def _client(ctx, login=True):
    con, _, tmp = ctx
    app = create_app(Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp), con, None)
    return owner_client(app) if login else TestClient(app)


def nonce(c):
    return c.post("/api/nonce").json()["nonce"]


def _visible(html: str) -> str:
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def _item(con, **kw):
    args = dict(label="도시락", meals_per_workday=2, meals_per_weekend_day=0, lead_days=1, safety_days=1,
                order_url="https://shop.example.com/o", remaining=0, now=T)
    args.update(kw)
    return supply.add_item(con, **args)


# --- 로그인 -------------------------------------------------------------------------------------
def test_pages_and_apis_require_owner(ctx):
    c = _client(ctx, login=False)
    for m, p in (("GET", "/routine"), ("GET", "/routine/settings"), ("POST", "/api/supply"),
                 ("POST", "/api/supply/x/delivered"), ("POST", "/api/rotation"), ("POST", "/api/proposal/x"),
                 ("POST", "/api/supply/x/delete")):
        r = c.request(m, p, json={} if m == "POST" else None, follow_redirects=False)
        assert r.status_code == 401, (m, p, r.status_code)


# --- 공급 API / 화면 ----------------------------------------------------------------------------
def test_supply_api_create_update_validation(ctx):
    con, _, _ = ctx
    c = _client(ctx)
    r = c.post("/api/supply", json={"label": "도시락", "meals_per_workday": 2, "meals_per_weekend_day": 0,
                                    "lead_days": 2, "safety_days": 1, "remaining": 6,
                                    "order_url": "https://a.example.com/x", "order_cutoff": "19:00"})
    assert r.status_code == 200
    iid = r.json()["id"]
    it = supply.get_item(con, iid)
    assert it.remaining == 6 and it.order_cutoff == "19:00"
    assert c.post(f"/api/supply/{iid}", json={"lead_days": 3}).status_code == 200
    assert supply.get_item(con, iid).lead_days == 3
    assert c.post(f"/api/supply/{iid}", json={"lead_days": 99}).status_code == 422
    assert c.post(f"/api/supply/{iid}", json={"label": "x", "bogus": 1}).status_code == 422
    assert c.post("/api/supply/none", json={"lead_days": 1}).status_code == 404
    assert c.post(f"/api/supply/{iid}/pause", json={"start": "2026-10-10", "until": "2026-10-12"}).status_code == 200
    assert supply.get_item(con, iid).paused_until == "2026-10-12"
    assert c.post(f"/api/supply/{iid}/pause", json={"start": "2026-10-13", "until": "2026-10-12"}).status_code == 422
    assert c.post(f"/api/supply/{iid}/resume").status_code == 200
    assert supply.get_item(con, iid).paused_from is None


def test_order_link_https_only_rendered(ctx):
    c = _client(ctx)
    base = {"label": "도시락", "meals_per_workday": 2, "meals_per_weekend_day": 0,
            "lead_days": 1, "safety_days": 1, "remaining": 0}
    assert c.post("/api/supply", json={**base, "order_url": "http://shop.example.com/x"}).status_code == 422
    iid = c.post("/api/supply", json=base).json()["id"]
    assert c.post(f"/api/supply/{iid}", json={"order_url": "javascript:alert(1)"}).status_code == 422
    h = c.get("/routine").text
    assert "주문하러 가기" not in h and 'href="http' not in h
    c.post(f"/api/supply/{iid}", json={"order_url": "https://shop.example.com/x"})
    h = c.get("/routine").text
    assert 'href="https://shop.example.com/x"' in h and "주문하러 가기" in h and "noopener" in h


def test_routine_page_flows(ctx):
    con, _, _ = ctx
    c = _client(ctx)
    iid = _item(con)
    assert c.post(f"/api/supply/{iid}/delivered", json={"qty": 10}).json()["remaining"] == 10
    v = _visible(c.get("/routine").text)
    assert re.search(coach_copy.REMAINING_LABEL + r"\s*10", v)
    for label in ("도착했어요", "오늘은 안 먹었어요", "남은 끼니 고치기", "주문했어요", "나중에"):
        assert label in v
    r1 = c.post(f"/api/supply/{iid}/ordered").json()
    assert r1["ask_cutoff"] is True and r1["order_url"] == "https://shop.example.com/o" and r1["order_id"]
    assert c.post(f"/api/supply/{iid}/cutoff", json={"cutoff": None}).status_code == 200
    assert c.post(f"/api/supply/{iid}/ordered").json()["ask_cutoff"] is False
    assert c.post(f"/api/supply/{iid}/cutoff", json={"cutoff": "25:00"}).status_code == 422
    assert c.post(f"/api/supply/{iid}/adjust", json={"remaining": 7}).json()["remaining"] == 7
    assert re.search(coach_copy.REMAINING_LABEL + r"\s*7", _visible(c.get("/routine").text))
    assert c.post(f"/api/supply/{iid}/snooze").status_code == 200
    assert c.post(f"/api/supply/{iid}/stale-ack").status_code == 200
    assert c.post(f"/api/supply/{iid}/skipped", json={}).status_code == 422   # 차감된 끼니가 없다


def test_cutoff_question_and_stale_check_rendered(ctx):
    con, _, _ = ctx
    c = _client(ctx)
    iid = _item(con, remaining=4)
    h = c.get("/routine").text
    assert re.search(r'class="askcutoff"\s+hidden', h)   # 주문 응답에서 ask_cutoff 일 때만 보인다
    con.execute("UPDATE supply_item SET created_at=? WHERE id=?", ("2026-09-01T00:00:00+00:00", iid))
    con.execute("INSERT INTO supply_event(id,item_id,kind,qty,for_date,created_at) VALUES('e1',?,'consumed',2,'2026-10-05','2026-10-05T00:00:00+00:00')", (iid,))
    assert coach_copy.STOCK_CHECK in c.get("/routine").text


def test_skipped_restores_consumed_meals(ctx):
    con, _, _ = ctx
    c = _client(ctx)
    iid = _item(con, remaining=10)
    supply.consume_through(con, datetime(2026, 10, 6).date(), now=T)
    left = supply.get_item(con, iid).remaining
    r = c.post(f"/api/supply/{iid}/skipped", json={"for_date": TODAY})
    assert r.status_code == 200 and supply.get_item(con, iid).remaining > left


def test_lead_proposal_shown_and_pass_leaves_lead_days(ctx):
    con, _, _ = ctx
    c = _client(ctx)
    iid = _item(con, lead_days=1)
    prop = routine_plan.propose(con, f"supply_lead:{iid}", 3, basis="도착 일수를 3일로 바꿀까요 제안", now=T)
    h = c.get("/routine").text
    assert "도착 일수를 3일로 바꿀까요 제안" in h
    for a in coach_copy.PROPOSAL_ACTIONS:
        assert a in h
    assert c.post(f"/api/proposal/{prop['id']}", json={"response": "passed"}).status_code == 200
    assert supply.get_item(con, iid).lead_days == 1
    assert "도착 일수를 3일로 바꿀까요 제안" not in c.get("/routine").text
    assert c.post(f"/api/proposal/{prop['id']}", json={"response": "passed"}).status_code == 422   # 이미 응답


def test_proposal_accept_changes_value(ctx):
    con, _, _ = ctx
    c = _client(ctx)
    iid = _item(con, lead_days=1)
    prop = routine_plan.propose(con, f"supply_lead:{iid}", 3, basis="b", now=T)
    assert c.post(f"/api/proposal/{prop['id']}", json={"response": "accepted"}).status_code == 200
    assert supply.get_item(con, iid).lead_days == 3
    assert c.post("/api/proposal/none", json={"response": "passed"}).status_code == 404


def test_supply_delete_hides_item(ctx):
    con, _, _ = ctx
    c = _client(ctx)
    iid = _item(con, label="사라질항목")
    assert "사라질항목" in c.get("/routine").text
    assert c.post(f"/api/supply/{iid}/delete", json={"nonce": nonce(c)}).status_code == 200
    assert "사라질항목" not in c.get("/routine").text
    assert supply.get_item(con, iid).active == 0


# --- 회전 후보 ----------------------------------------------------------------------------------
def test_rotation_requires_verified_and_https(ctx):
    con, _, _ = ctx
    c = _client(ctx)
    iid = _item(con)
    body = {"item_id": iid, "label": "새 구성", "url": "https://x.example.com/n", "notes": "메모"}
    assert c.post("/api/rotation", json=body).status_code == 422
    assert c.post("/api/rotation", json={**body, "verified": False}).status_code == 422
    assert c.post("/api/rotation", json={**body, "verified": True, "url": "http://x"}).status_code == 422
    assert c.post("/api/rotation", json={**body, "verified": True, "item_id": "none"}).status_code == 404
    r = c.post("/api/rotation", json={**body, "verified": True})
    assert r.status_code == 200 and r.json()["id"]
    assert c.post("/api/rotation/dismiss").status_code == 200






# --- 화면 공통 ----------------------------------------------------------------------------------










def test_pages_require_login(ctx):
    c = _client(ctx, login=False)
    for p in ("/routine", "/routine/settings"):
        assert c.get(p, follow_redirects=False).status_code == 401










def test_routine_uses_real_css_tokens(ctx):
    css = (STATIC / "tokens.css").read_text(encoding="utf-8") + (STATIC / "app.css").read_text(encoding="utf-8")
    c = _client(ctx)
    _item(ctx[0])
    h = c.get("/routine").text + c.get("/routine/settings").text
    for tok in set(re.findall(r"var\((--[\w-]+)\)", h)):
        assert tok + ":" in css, tok
    assert "btn-primary" in h

"""QR 기기 등록: PC(루프백)에서 QR 을 만들고, 폰이 찍어 등록하면 서명된 쿠키(180일)로 소유자가 된다."""
import re

import pytest
from conftest import owner_client
from fastapi.testclient import TestClient

from pa.core import devices
from pa.settings import Settings
from pa.web.app import create_app


class FakeRouter:
    tone = "polite"
    honorific = ""


@pytest.fixture
def env(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path, public_url="https://home.example.test")
    app = create_app(s, con, FakeRouter())
    phone = TestClient(app, base_url="http://localhost:8010", client=("203.0.113.9", 5000), follow_redirects=False)
    return app, owner_client(app, follow_redirects=False), phone, con


def _token(con):
    return con.execute("SELECT value FROM auth_nonce WHERE purpose='join' ORDER BY expires_at DESC").fetchone()[0]


def test_pc_sees_devices_page_without_a_qr_until_asked(env):
    _, pc, _, _ = env
    r = pc.get("/devices")
    assert r.status_code == 200 and "휴대폰 추가" in r.text and "<svg" not in r.text


def test_stranger_cannot_open_devices_or_make_qr(env):
    _, _, phone, _ = env
    assert phone.get("/devices").status_code == 401
    assert phone.post("/devices/new").status_code == 401


def test_pc_makes_a_qr_with_a_one_time_token(env):
    _, pc, _, con = env
    r = pc.post("/devices/new")
    assert r.status_code == 200 and "<svg" in r.text and "5분" in r.text
    t = _token(con)
    assert t not in r.text                                   # 토큰을 글자로 드러내지 않는다(QR 안에만)
    assert r.headers["cache-control"] == "no-store"


def test_without_a_reachable_address_no_qr_is_made(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path, allowed_hosts=["localhost", "127.0.0.1"])
    pc = owner_client(create_app(s, con, FakeRouter()))
    r = pc.post("/devices/new")
    assert r.status_code == 200 and "<svg" not in r.text and "Tailscale" in r.text


def test_join_page_does_not_use_up_the_token(env):
    _, pc, phone, con = env
    pc.post("/devices/new")
    t = _token(con)
    assert phone.get(f"/join/{t}").status_code == 200
    assert phone.get(f"/join/{t}").status_code == 200        # 미리보기·새로고침으로 토큰이 사라지지 않는다
    assert devices.consume_join_token(con, t) is True


def test_bad_or_expired_token_gets_a_friendly_410(env):
    _, _, phone, con = env
    r = phone.get("/join/nope")
    assert r.status_code == 410 and "다시" in r.text
    assert phone.post("/join/nope", data={"name": "x"}).status_code == 410
    assert con.execute("SELECT COUNT(*) FROM device").fetchone()[0] == 0


def test_phone_joins_and_becomes_owner(env):
    _, pc, phone, con = env
    pc.post("/devices/new")
    t = _token(con)
    assert phone.get("/tasks").status_code == 401             # 등록 전
    r = phone.post(f"/join/{t}", data={"name": "엄마 아이폰"})
    assert r.status_code == 303 and r.headers["location"] == "/install"
    assert phone.get("/tasks").status_code == 200             # 등록 뒤에는 쿠키로 들어간다
    assert [d["name"] for d in devices.list_devices(con)] == ["엄마 아이폰"]
    assert "엄마 아이폰" in pc.get("/devices").text


def test_token_works_once_only(env):
    app, pc, phone, con = env
    pc.post("/devices/new")
    t = _token(con)
    phone.post(f"/join/{t}", data={"name": "폰1"})
    other = TestClient(app, base_url="http://localhost:8010", client=("203.0.113.10", 5000), follow_redirects=False)
    assert other.post(f"/join/{t}", data={"name": "폰2"}).status_code == 410
    assert other.get("/tasks").status_code == 401
    assert len(devices.list_devices(con)) == 1


def test_cookie_lasts_180_days_and_is_httponly(env):
    _, pc, phone, con = env
    pc.post("/devices/new")
    r = phone.post(f"/join/{_token(con)}", data={"name": "폰"})
    cookie = r.headers["set-cookie"].lower()
    assert "pa_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert f"max-age={180 * 24 * 3600}" in cookie


def test_revoked_device_loses_access_immediately(env):
    _, pc, phone, con = env
    pc.post("/devices/new")
    phone.post(f"/join/{_token(con)}", data={"name": "폰"})
    assert phone.get("/tasks").status_code == 200
    did = devices.list_devices(con)[0]["id"]
    assert pc.post(f"/devices/{did}/revoke").status_code == 303
    assert phone.get("/tasks").status_code == 401
    assert phone.post("/api/tasks", json={"title": "x"}).status_code == 401


def test_revoke_unknown_device_is_404_and_phone_cannot_be_revoked_by_strangers(env):
    _, pc, phone, con = env
    assert pc.post("/devices/nope/revoke").status_code == 404
    pc.post("/devices/new")
    phone.post(f"/join/{_token(con)}", data={"name": "폰"})
    did = devices.list_devices(con)[0]["id"]
    stranger = TestClient(phone.app, base_url="http://localhost:8010", client=("203.0.113.55", 5000), follow_redirects=False)
    assert stranger.post(f"/devices/{did}/revoke").status_code == 401
    assert devices.is_active(con, did)


def test_owner_cookie_without_a_registered_device_is_refused(env):
    app, _, _, _ = env
    import base64
    import json
    from itsdangerous import TimestampSigner
    data = base64.b64encode(json.dumps({"owner": True}).encode())
    cookie = TimestampSigner("t" * 32).sign(data).decode()
    c = TestClient(app, base_url="http://localhost:8010", client=("203.0.113.9", 5000), follow_redirects=False)
    c.cookies.set("pa_session", cookie)
    assert c.get("/tasks").status_code == 401
    data = base64.b64encode(json.dumps({"owner": True, "device": "ghost"}).encode())
    c.cookies.set("pa_session", TimestampSigner("t" * 32).sign(data).decode())
    assert c.get("/tasks").status_code == 401


def test_device_name_is_cleaned_on_join(env):
    _, pc, phone, con = env
    pc.post("/devices/new")
    phone.post(f"/join/{_token(con)}", data={"name": "<script>alert(1)</script>"})
    name = devices.list_devices(con)[0]["name"]
    assert "<" not in name and ">" not in name
    assert "<script>" not in pc.get("/devices").text


def test_join_and_devices_pages_hide_the_tab_bar_of_the_app(env):
    _, pc, phone, con = env
    pc.post("/devices/new")
    assert 'class="tabbar"' not in phone.get(f"/join/{_token(con)}").text


def test_settings_and_welcome_done_link_to_devices(env):
    _, pc, _, _ = env
    assert "/devices" in pc.get("/settings").text
    assert "/devices" in pc.get("/welcome/done").text


def test_phone_owner_can_also_use_devices_page(env):
    _, pc, phone, con = env
    pc.post("/devices/new")
    phone.post(f"/join/{_token(con)}", data={"name": "폰"})
    assert phone.get("/devices").status_code == 200


def test_revoke_removes_push_subscriptions(env):
    _, pc, phone, con = env
    pc.post("/devices/new")
    phone.post(f"/join/{_token(con)}", data={"name": "폰"})
    did = devices.list_devices(con)[0]["id"]
    con.execute("INSERT INTO push_subscription(id,device_id,endpoint,p256dh,auth,created_at) VALUES('s',?,?,?,?,'t')", (did, "https://p/1", "k", "a"))
    pc.post(f"/devices/{did}/revoke")
    assert con.execute("SELECT COUNT(*) FROM push_subscription").fetchone()[0] == 0


def test_join_rejects_cross_site_post(env):
    _, pc, phone, con = env
    pc.post("/devices/new")
    r = phone.post(f"/join/{_token(con)}", data={"name": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert devices.list_devices(con) == []


def test_tailscale_guard_accepts_several_family_logins(con, tmp_path):
    s = Settings(session_secret="t" * 32, allowed_tailscale_login="mom@example.com, dad@example.com", data_dir=tmp_path)
    app = create_app(s, con, FakeRouter())
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.get("/join/nope", headers={"Tailscale-User-Login": "dad@example.com"}).status_code == 410   # 가드를 통과해 토큰 검사까지 감
    assert c.get("/join/nope", headers={"Tailscale-User-Login": "mom@example.com"}).status_code == 410
    assert c.get("/join/nope", headers={"Tailscale-User-Login": "stranger@example.com"}).status_code == 403
    assert c.get("/join/nope").status_code == 403

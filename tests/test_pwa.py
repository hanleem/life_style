"""홈 화면에 추가(PWA): 매니페스트·서비스 워커·아이콘·설치 안내·알림 구독."""
import json
import struct
from pathlib import Path

import pytest
from conftest import owner_client
from fastapi.testclient import TestClient

from pa.core import devices, profile, push
from pa.settings import Settings
from pa.web.app import create_app

ICONS = Path(__file__).resolve().parents[1] / "pa/web/static/icons"
SUB = {"endpoint": "https://web.push.apple.com/abc123", "keys": {"p256dh": "BPubKey_-1", "auth": "AuthKey_-1"}}


class FakeRouter:
    tone = "polite"
    honorific = ""


@pytest.fixture(autouse=True)
def _reset_push():
    yield
    push.configure(None)


@pytest.fixture
def env(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path, public_url="https://home.example.test")
    app = create_app(s, con, FakeRouter())
    phone = TestClient(app, base_url="http://localhost:8010", client=("203.0.113.9", 5000), follow_redirects=False)
    pc = owner_client(app, follow_redirects=False)
    return app, pc, phone, con, tmp_path


def _join(pc, phone, con):
    pc.post("/devices/new")
    t = con.execute("SELECT value FROM auth_nonce WHERE purpose='join'").fetchone()[0]
    phone.post(f"/join/{t}", data={"name": "폰"})
    return devices.list_devices(con)[0]["id"]


def test_manifest_has_what_ios_and_android_need(env):
    _, _, phone, _, _ = env
    r = phone.get("/manifest.webmanifest")               # 로그인 전에도 읽혀야 브라우저가 앱으로 설치할 수 있다
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["name"] == "가족 비서" and m["short_name"] and m["start_url"] == "/" and m["scope"] == "/"
    assert m["display"] == "standalone" and m["lang"] == "ko"
    sizes = {(i["sizes"], i.get("purpose", "any")) for i in m["icons"]}
    assert ("192x192", "any") in sizes and ("512x512", "any") in sizes and ("512x512", "maskable") in sizes
    for i in m["icons"]:
        assert phone.get(i["src"]).status_code == 200


def test_manifest_colors_follow_the_chosen_theme(env):
    _, _, phone, _, tmp_path = env
    profile.save(tmp_path, {**profile.defaults(), "theme": "wood"})
    wood = phone.get("/manifest.webmanifest").json()
    profile.save(tmp_path, {**profile.defaults(), "theme": "mono"})
    mono = phone.get("/manifest.webmanifest").json()
    assert wood["theme_color"] != mono["theme_color"] and wood["theme_color"].startswith("#")


def test_service_worker_handles_push_and_clicks_and_caches_nothing(env):
    _, _, phone, _, _ = env
    r = phone.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["service-worker-allowed"] == "/"
    js = r.text
    assert "addEventListener('push'" in js and "showNotification" in js and "notificationclick" in js and "clients.openWindow" in js
    assert "caches" not in js                              # 개인 화면은 저장하지 않는다
    assert "http://" not in js and "https://" not in js    # 클릭 이동은 같은 사이트 안의 경로만


def test_service_worker_only_opens_same_site_paths(env):
    js = env[2].get("/sw.js").text
    assert "startsWith('/')" in js and "startsWith('//')" in js


def test_pages_link_the_manifest_and_icons(env):
    _, pc, _, _, _ = env
    html = pc.get("/").text
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png"' in html
    assert 'name="apple-mobile-web-app-capable"' in html and 'name="theme-color"' in html


def _png_size(p: Path):
    b = p.read_bytes()
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", b[16:24])


def test_icons_exist_with_the_right_sizes():
    assert _png_size(ICONS / "icon-192.png") == (192, 192) and _png_size(ICONS / "icon-512.png") == (512, 512)
    assert _png_size(ICONS / "icon-maskable-512.png") == (512, 512) and _png_size(ICONS / "apple-touch-icon.png") == (180, 180)
    assert (ICONS / "icon.svg").read_text(encoding="utf-8").startswith("<svg")


def test_saved_icons_match_the_generator():
    from tools import gen_icons
    for name, data in gen_icons.build().items():
        assert (ICONS / name).read_bytes() == data, f"{name} 이(가) tools/gen_icons.py 결과와 달라요 — 다시 실행해 주세요"


def test_install_guide_is_for_owners_only(env):
    _, _, phone, _, _ = env
    assert phone.get("/install").status_code == 401


def test_install_guide_explains_iphone_steps_and_has_the_notification_button(env):
    app, pc, phone, con, _ = env
    _join(pc, phone, con)
    r = phone.get("/install")
    assert r.status_code == 200
    for needle in ("공유", "홈 화면에 추가", "알림 켜기", "Safari"):
        assert needle in r.text, needle
    pub = push.ensure_keys(app.state.settings.data_dir)
    assert pub in r.text                                    # 브라우저가 구독할 때 쓸 공개키
    assert "pa_session" not in r.text and "private" not in r.text.lower()


def test_subscribe_stores_for_the_current_device(env):
    _, pc, phone, con, _ = env
    did = _join(pc, phone, con)
    r = phone.post("/api/push/subscribe", json=SUB)
    assert r.status_code == 200 and r.json()["ok"]
    assert con.execute("SELECT device_id FROM push_subscription").fetchone()[0] == did


def test_pc_can_subscribe_too(env):
    _, pc, _, con, _ = env
    assert pc.post("/api/push/subscribe", json=SUB).status_code == 200
    assert con.execute("SELECT device_id FROM push_subscription").fetchone()[0] == "pc"


def test_subscribe_rejects_bad_payloads_and_strangers(env):
    _, pc, phone, con, _ = env
    assert phone.post("/api/push/subscribe", json=SUB).status_code == 401
    assert pc.post("/api/push/subscribe", json={"endpoint": "https://evil.example.com/x", "keys": SUB["keys"]}).status_code == 400
    assert pc.post("/api/push/subscribe", json={"nope": 1}).status_code == 400
    assert pc.post("/api/push/subscribe", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
    assert con.execute("SELECT COUNT(*) FROM push_subscription").fetchone()[0] == 0


def test_unsubscribe(env):
    _, pc, phone, con, _ = env
    _join(pc, phone, con)
    phone.post("/api/push/subscribe", json=SUB)
    assert phone.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]}).json() == {"ok": True}
    assert con.execute("SELECT COUNT(*) FROM push_subscription").fetchone()[0] == 0
    assert phone.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]}).json() == {"ok": False}


def test_app_start_creates_vapid_keys_and_configures_push(env):
    _, _, _, _, tmp_path = env
    assert (tmp_path / push.KEY_FILE).exists()
    assert push._cfg is not None and push._cfg.data_dir == tmp_path


def test_settings_link_to_install_guide(env):
    _, pc, _, _, _ = env
    assert "/install" in pc.get("/settings").text

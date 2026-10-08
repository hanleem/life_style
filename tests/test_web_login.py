"""소유자 인정: 이 PC(직접 루프백)의 브라우저만 로그인 없이 들어온다. 그 외는 401."""
from conftest import owner_client
from fastapi.testclient import TestClient

from pa.settings import Settings
from pa.web.app import create_app


def _app(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    return create_app(s, con, None)


def test_loopback_browser_is_the_owner(con, tmp_path):
    c = owner_client(_app(con, tmp_path))
    assert c.get("/approvals").status_code == 200
    assert c.get("/routine").status_code == 200


def test_non_loopback_client_is_not_the_owner(con, tmp_path):
    c = TestClient(_app(con, tmp_path))   # base_url=testserver: 루프백 Host 가 아니다
    assert c.get("/approvals").status_code == 401
    assert c.post("/api/nonce").status_code == 401


def test_proxy_headers_are_not_treated_as_loopback(con, tmp_path):
    c = owner_client(_app(con, tmp_path))
    r = c.get("/approvals", headers={"X-Forwarded-For": "100.64.0.9"})
    assert r.status_code == 401


def test_cross_site_post_is_rejected(con, tmp_path):
    c = owner_client(_app(con, tmp_path))
    assert c.post("/api/nonce", headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/nonce", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert c.post("/api/nonce", headers={"sec-fetch-site": "same-origin"}).status_code == 200

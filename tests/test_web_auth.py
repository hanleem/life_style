from datetime import timedelta

import pytest
from conftest import owner_client
from fastapi.testclient import TestClient
from freezegun import freeze_time

from pa.clock import utcnow
from pa.core.errors import Forbidden
from pa.settings import Settings
from pa.web.app import create_app
from pa.web.auth import consume_nonce


def _app(con, skip=False):
    s = Settings(session_secret="t" * 32, allowed_tailscale_login="family@example.com",
                 dev_skip_tailscale=skip)
    return create_app(s, con, router=None)


def test_tailscale_header_required_off_loopback(con):
    c = TestClient(_app(con))   # testserver 는 루프백이 아니므로 Tailscale 헤더가 필요하다
    assert c.post("/api/nonce").status_code == 403
    assert c.get("/health").status_code == 200


def test_loopback_owner_gets_nonce(con):
    c = owner_client(_app(con))
    assert c.post("/api/nonce").status_code == 200


def test_proxy_headers_disable_loopback_shortcut(con):
    c = owner_client(_app(con))
    assert c.post("/api/nonce", headers={"X-Forwarded-For": "100.64.0.9"}).status_code == 403


def test_nonce_single_use_and_expiry(con):
    c = owner_client(_app(con))
    n = c.post("/api/nonce").json()["nonce"]
    consume_nonce(con, n)
    with pytest.raises(Forbidden):
        consume_nonce(con, n)
    n2 = c.post("/api/nonce").json()["nonce"]
    with freeze_time(utcnow() + timedelta(minutes=3)):
        with pytest.raises(Forbidden):
            consume_nonce(con, n2)

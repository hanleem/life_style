from conftest import owner_client
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from pa.core.errors import Forbidden
from pa.integrations import google_auth as ga
from pa.settings import Settings
from pa.web.app import create_app

BASE = ga.SCOPES["base"]


def _secret_file(tmp_path):
    p = tmp_path / "client_secret.json"
    p.write_text(json.dumps({"web": {
        "client_id": "cid", "client_secret": "csec",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost:8010/auth/google/callback"]}}))
    return p


def _settings(tmp_path, key=True, dev=True, secret=True):
    return Settings(session_secret="t" * 32, dev_skip_tailscale=dev,
                    allowed_tailscale_login="me@x", data_dir=tmp_path,
                    google_token_key=Fernet.generate_key().decode() if key else "",
                    google_client_secret_path=_secret_file(tmp_path) if secret else tmp_path / "missing.json")


def _client(s, con, host):
    c = owner_client(create_app(s, con, None), base_url="http://localhost:8010", client=(host, 5000))
    return c


@pytest.fixture
def settings(tmp_path):
    return _settings(tmp_path)


@pytest.fixture
def loopback_client(settings, con):
    return _client(settings, con, "127.0.0.1")


@pytest.fixture
def app_client_remote(settings, con):
    return _client(settings, con, "100.64.0.5")


@pytest.fixture
def app_client_no_key(tmp_path, con):
    return _client(_settings(tmp_path, key=False), con, "127.0.0.1")


def _state(url):
    return dict(x.split("=", 1) for x in url.split("?", 1)[1].split("&"))["state"]


def _fake_exchange(monkeypatch, scopes=None, seen=None):
    def ex(self, code, verifier, sc):
        if seen is not None:
            seen.update(code=code, verifier=verifier, scopes=sc)
        return {"token": "t", "refresh_token": "r", "scopes": scopes or sc, "expiry": None}
    monkeypatch.setattr(ga.GoogleAuth, "_exchange", ex)


def test_callback_rejects_non_loopback(app_client_remote):
    assert app_client_remote.get("/auth/google/callback?state=s&code=c").status_code == 403


def test_state_single_use_and_expiry(loopback_client, con, monkeypatch):
    seen = {}
    _fake_exchange(monkeypatch, seen=seen)
    url = loopback_client.get("/auth/google/start?level=base", follow_redirects=False).headers["location"]
    assert "code_challenge=" in url and "code_challenge_method=S256" in url
    state = _state(url)
    r = loopback_client.get(f"/auth/google/callback?state={state}&code=c")
    assert r.status_code == 200 and "연결되었습니다" in r.text
    assert seen["verifier"] and len(seen["verifier"]) >= 43
    assert loopback_client.get(f"/auth/google/callback?state={state}&code=c").status_code == 400
    url2 = loopback_client.get("/auth/google/start?level=base", follow_redirects=False).headers["location"]
    s2 = _state(url2)
    con.execute("UPDATE google_oauth_state SET expires_at=? WHERE state=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), s2))
    assert loopback_client.get(f"/auth/google/callback?state={s2}&code=c").status_code == 400
    assert loopback_client.get("/auth/google/callback?state=bogus&code=c").status_code == 400


def test_start_requests_incremental_scopes(loopback_client):
    url = loopback_client.get("/auth/google/start?level=calwrite", follow_redirects=False).headers["location"]
    assert "calendar.readonly" in url and "calendar.events" in url and "mail.google.com" not in url
    assert "gmail" not in url
    assert "access_type=offline" in url and "include_granted_scopes=true" in url and "prompt=consent" in url
    base_url = loopback_client.get("/auth/google/start?level=base", follow_redirects=False).headers["location"]
    assert "calendar.events" not in base_url


def test_start_includes_previously_granted(loopback_client, monkeypatch):
    _fake_exchange(monkeypatch)
    s = _state(loopback_client.get("/auth/google/start?level=calwrite", follow_redirects=False).headers["location"])
    loopback_client.get(f"/auth/google/callback?state={s}&code=c")
    url = loopback_client.get("/auth/google/start?level=base", follow_redirects=False).headers["location"]
    assert "calendar.events" in url and "calendar.readonly" in url


def test_start_unknown_level_and_login(loopback_client, settings, con):
    assert loopback_client.get("/auth/google/start?level=nope", follow_redirects=False).status_code == 400
    anon = TestClient(create_app(settings, con, None))   # 루프백이 아닌 접속
    assert anon.get("/auth/google/start?level=base", follow_redirects=False).status_code == 401
    assert anon.get("/settings/google").status_code == 401


def test_settings_shows_missing_key(app_client_no_key):
    assert "PA_GOOGLE_TOKEN_KEY" in app_client_no_key.get("/settings/google").text
    assert app_client_no_key.get("/auth/google/start?level=base", follow_redirects=False).status_code == 400


def test_settings_shows_missing_client_secret(tmp_path, con):
    c = _client(_settings(tmp_path, secret=False), con, "127.0.0.1")
    assert "client_secret" in c.get("/settings/google").text
    assert c.get("/auth/google/start?level=base", follow_redirects=False).status_code == 400


def test_forbidden_scope_not_saved(settings, con, monkeypatch):
    _fake_exchange(monkeypatch, scopes=BASE + ["https://mail.google.com/"])
    g = ga.GoogleAuth(settings, con)
    state = _state(g.start(["base"]))
    with pytest.raises(Forbidden):
        g.finish(state, "c")
    assert not g.connected()


def test_guard_exception_only_for_callback(tmp_path, con):
    s = _settings(tmp_path, dev=False)
    app = create_app(s, con, None)
    c = TestClient(app, base_url="http://localhost:8010", client=("127.0.0.1", 5000))
    assert c.get("/auth/google/callback?state=x&code=c").status_code == 400   # 직접 루프백: 가드 통과
    assert c.get("/settings/google").status_code == 200                     # 이 PC 의 브라우저는 소유자
    r = TestClient(app, client=("100.64.0.5", 1))
    assert r.get("/auth/google/callback?state=x&code=c").status_code == 403
    assert r.get("/settings/google").status_code == 403


def test_settings_page_and_calendar_write(loopback_client, con, monkeypatch):
    _fake_exchange(monkeypatch)
    s = _state(loopback_client.get("/auth/google/start?level=calwrite", follow_redirects=False).headers["location"])
    loopback_client.get(f"/auth/google/callback?state={s}&code=c")
    html = loopback_client.get("/settings/google").text
    assert "Google 캘린더 연결" in html and "캘린더 쓰기 켜기" in html
    assert "이 PC의 브라우저" in html and 'name="ids"' in html
    r = loopback_client.post("/settings/google/calendar-write", data={"ids": ["primary", "x@group"]},
                             follow_redirects=False)
    assert r.status_code == 303
    row = con.execute("SELECT value FROM preference WHERE key='calendar.write_ids'").fetchone()
    assert json.loads(row["value"]) == ["primary", "x@group"]


async def test_access_token_refresh(settings, con, monkeypatch):
    g = ga.GoogleAuth(settings, con)
    g.store.save({"token": "old", "refresh_token": "r", "scopes": BASE,
                  "expiry": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})

    def fake_refresh(self, request):
        self.token = "new"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fake_refresh)
    assert await g.access_token() == "new"
    assert g.store.load()["token"] == "new"
    assert await g.access_token() == "new"


async def test_access_token_refresh_failure_none(settings, con, monkeypatch):
    g = ga.GoogleAuth(settings, con)
    g.store.save({"token": "old", "refresh_token": "r", "scopes": BASE,
                  "expiry": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})

    def boom(self, request):
        raise RuntimeError("secret-detail")
    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", boom)
    assert await g.access_token() is None


def test_callback_rejects_proxied_and_foreign_host(settings, con):
    c = _client(settings, con, "127.0.0.1")
    for h in ({"Tailscale-User-Login": "me@x"}, {"X-Forwarded-For": "100.64.0.5"}, {"Forwarded": "for=1.2.3.4"}):
        assert c.get("/auth/google/callback?state=x&code=c", headers=h).status_code == 403
    assert c.get("/auth/google/callback?state=x&code=c", headers={"Host": "pa.tailnet.example"}).status_code == 403
    assert c.get("/auth/google/callback?state=x&code=c", headers={"Host": "127.0.0.1:8010"}).status_code == 400
    v6 = _client(settings, con, "::ffff:127.0.0.1")
    assert v6.get("/auth/google/callback?state=x&code=c").status_code == 400


def _stub_fetch(monkeypatch, scope):
    from google_auth_oauthlib.flow import Flow

    def fetch(self, **kw):
        self.oauth2session.token = {"access_token": "t", "refresh_token": "r", "scope": scope,
                                    "expires_in": 3600, "expires_at": __import__("time").time() + 3600,
                                    "token_type": "Bearer"}
    monkeypatch.setattr(Flow, "fetch_token", fetch)


def test_exchange_reports_granted_not_requested(settings, con, monkeypatch):
    sub = BASE[0]
    _stub_fetch(monkeypatch, sub)
    g = ga.GoogleAuth(settings, con)
    info = g._exchange("c", "v" * 50, BASE + ga.SCOPES["calwrite"])
    assert info["scopes"] == [sub]


def test_finish_real_exchange_refuses_forbidden(settings, con, monkeypatch):
    _stub_fetch(monkeypatch, " ".join(BASE + ["https://mail.google.com/"]))
    g = ga.GoogleAuth(settings, con)
    with pytest.raises(Forbidden):
        g.finish(_state(g.start(["base"])), "c")
    assert not g.connected()


def test_finish_partial_consent_stored(settings, con, monkeypatch):
    _stub_fetch(monkeypatch, " ".join(BASE))
    g = ga.GoogleAuth(settings, con)
    assert g.finish(_state(g.start(["base", "calwrite"])), "c") == BASE
    assert g.granted() == BASE


MODIFY = "https://www.googleapis.com/auth/gmail.modify"


def test_start_does_not_rerequest_foreign_granted_scopes(settings, con):
    g = ga.GoogleAuth(settings, con)
    g.store.save({"token": "t", "refresh_token": "r", "scopes": BASE + [MODIFY, "openid"]})
    assert MODIFY in g.granted()
    url = g.start(["calwrite"])
    assert "gmail.modify" not in url and "openid" not in url and "calendar.events" in url
    stored = json.loads(con.execute("SELECT scopes FROM google_oauth_state").fetchone()["scopes"])
    assert set(stored) <= ga.ALLOWED_SCOPES


def test_finish_never_saves_modify_or_send(settings, con, monkeypatch):
    assert {MODIFY, "https://www.googleapis.com/auth/gmail.send"} <= ga.FORBIDDEN_SCOPES
    _fake_exchange(monkeypatch, scopes=BASE + [MODIFY])
    g = ga.GoogleAuth(settings, con)
    with pytest.raises(Forbidden):
        g.finish(_state(g.start(["base"])), "c")
    assert not g.connected()


def test_finish_drops_unknown_scopes(settings, con, monkeypatch):
    _fake_exchange(monkeypatch, scopes=BASE + ["openid"])
    g = ga.GoogleAuth(settings, con)
    assert g.finish(_state(g.start(["base"])), "c") == BASE
    assert g.granted() == BASE


def test_write_allowlist_default_agrees_across_consumers(loopback_client, settings, con, monkeypatch):
    from pa.core.calendar_actions import write_allowlist
    from pa.main import pick_calendars
    _fake_exchange(monkeypatch)
    s = _state(loopback_client.get("/auth/google/start?level=calwrite", follow_redirects=False).headers["location"])
    loopback_client.get(f"/auth/google/callback?state={s}&code=c")
    # unset preference: empty everywhere
    assert write_allowlist(con) == []
    html = loopback_client.get("/settings/google").text
    assert 'value="primary" checked' not in html
    _, write = pick_calendars(ga.GoogleAuth(settings, con), con)
    assert write._write_ids() == []
    # explicit preference: all three agree
    con.execute("INSERT INTO preference(key,value,updated_at) VALUES('calendar.write_ids',?,?)",
                (json.dumps(["primary"]), "2026-10-06T00:00:00+00:00"))
    assert write_allowlist(con) == ["primary"]
    assert 'value="primary" checked' in loopback_client.get("/settings/google").text
    assert write._write_ids() == ["primary"]

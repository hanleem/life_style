import pytest

from pa.db import connect, init_db


@pytest.fixture(autouse=True)
def _no_holidays_leak():
    """휴일 캘린더 값은 프로세스 전역이라 테스트 사이에 새지 않게 비운다."""
    from pa.core import supply
    supply.set_holidays(())
    yield
    supply.set_holidays(())


@pytest.fixture(autouse=True)
def _baseline_rules(request, monkeypatch):
    """테스트는 실제 일정(config/settings.yaml)이 아니라 고정된 기준 일정을 쓴다.
    일정이 바뀔 때마다 로직 테스트가 깨지지 않게 한다. 실제 설정을 검증하는 테스트는 marker real_config."""
    if request.node.get_closest_marker("real_config"):
        return
    from pathlib import Path
    from pa import rules
    monkeypatch.setattr(rules, "DEFAULT_PATH", Path(__file__).parent / "fixtures" / "settings_baseline.yaml")


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch):
    """샌드박스/CI 에서 가짜 호스트(a.test 등)가 gaierror 로 막히지 않도록, 해석 실패 시 공인 IP 로 대체한다.
    (urlcheck 는 해석 불가 호스트를 거부한다. 해당 동작은 loop.getaddrinfo 를 직접 패치해 별도 테스트한다.)"""
    import socket
    real = socket.getaddrinfo

    def fake(host, port, *a, **k):
        try:
            return real(host, port, *a, **k)
        except socket.gaierror:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "core.db")
    init_db(c)
    yield c
    c.close()


async def approve_latest(con, draft_id):
    """현재 버전·payload_hash로 approve 호출(테스트 헬퍼). action_id 반환."""
    from pa.core import drafts
    from pa.core.approval import approve
    v = drafts.current_version(con, draft_id)
    return await approve(con, draft_id=draft_id, version=v["version"], hash_prefix=v["payload_hash"][:12],
                         approver="test", ttl_minutes=30)


@pytest.fixture(autouse=True)
def _test_allowed_host(monkeypatch):
    """TestClient 의 기본 Host(testserver) 허용. 운영 기본값은 건드리지 않고 테스트 환경에서만 추가."""
    monkeypatch.setenv("PA_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")


@pytest.fixture(autouse=True)
def _test_peers(monkeypatch):
    """TestClient 의 상대(testclient/testserver)만 테스트에서 허용. 운영 ALLOWED_PEERS 는 루프백뿐."""
    from pa.web import auth
    monkeypatch.setattr(auth, "ALLOWED_PEERS", auth.LOOPBACK | {"testclient", "testserver", "localhost"})


@pytest.fixture(autouse=True)
def _no_profile_gate(monkeypatch, tmp_path):
    """대부분의 화면 테스트는 인터뷰를 마친 뒤의 앱을 본다. 게이트 자체는 test_welcome 이 Settings(require_profile=True)로 검증한다."""
    monkeypatch.setenv("PA_REQUIRE_PROFILE", "false")
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "pa-data"))   # 개발 PC 의 실제 data/ 를 읽지 않게


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """개발자 .env 가 테스트를 흔들지 않게."""
    from pydantic_settings import SettingsConfigDict
    from pa.settings import Settings
    monkeypatch.setitem(Settings.model_config, "env_file", None)


def owner_client(app, **kw):
    """이 PC(직접 루프백)의 브라우저처럼 접속하는 TestClient — 소유자로 인정된다."""
    from fastapi.testclient import TestClient
    kw.setdefault("base_url", "http://localhost:8010")
    kw.setdefault("client", ("127.0.0.1", 5000))
    return TestClient(app, **kw)


def cal_payload(title="가족 모임", hour=5, calendar_id="primary") -> dict:
    return {"calendar_id": calendar_id, "title": title, "start": f"2026-10-07T{hour:02d}:00:00+00:00",
            "end": f"2026-10-07T{hour + 1:02d}:00:00+00:00"}


def calendar_draft(con, title="가족 모임", hour=5, allow=True) -> str:
    """쓰기 허용 캘린더를 켜고 일정 초안(승인 대기) 하나를 만든다."""
    from pa.core.calendar_actions import create_calendar_draft
    if allow:
        con.execute("INSERT OR REPLACE INTO preference(key,value) VALUES('calendar.write_ids','[\"primary\"]')")
    return create_calendar_draft(con, cal_payload(title, hour))


def freeze_clock(monkeypatch, when):
    """웹 화면들이 쓰는 시계(각 라우트 모듈의 _now)와 pa.clock 을 같은 시각으로 고정한다."""
    from pa import clock
    from pa.web import routes, routes_health, routes_routine, routes_scout, routes_tasks, routes_voice
    for mod in (routes, routes_routine, routes_voice, routes_health, routes_scout):
        monkeypatch.setattr(mod, "_now", lambda: when)
    monkeypatch.setattr(routes_tasks, "_now", lambda: when)
    monkeypatch.setattr(clock, "utcnow", lambda: when)

from conftest import owner_client
"""/settings/health: 백업 상태 한 줄(소유자 전용, 경로·이름 노출 없음)."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pa.settings import Settings
from pa.web import routes_health
from pa.web.app import create_app

NOW = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)       # 10/7 10:00 KST
LAST = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)     # 10/7 03:00 KST


def _pref(con, k, v):
    con.execute("INSERT INTO preference(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v, "x"))


def _client(con, tmp_path, *, key="K" * 44, login=True, monkeypatch=None):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True,
                 data_dir=tmp_path / "data", backup_key=key, _env_file=None)
    c = (owner_client if login else TestClient)(create_app(s, con, router=None))
    return c


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(routes_health, "_now", lambda: NOW)


def test_health_requires_owner(con, tmp_path):
    c = _client(con, tmp_path, login=False)
    assert c.get("/settings/health").status_code in (401, 403)


def test_health_shows_off_without_key(con, tmp_path):
    r = _client(con, tmp_path, key="").get("/settings/health")
    assert r.status_code == 200 and "백업이 꺼져 있습니다" in r.text


def test_health_shows_never(con, tmp_path):
    r = _client(con, tmp_path).get("/settings/health")
    assert "아직 백업이 없습니다" in r.text and "백업을 확인해 주세요" not in r.text


def test_health_shows_last_ok_kst_and_size(con, tmp_path):
    _pref(con, "backup.last_ok_at", LAST.isoformat())
    _pref(con, "backup.last_bytes", "4404019")
    _pref(con, "backup.last_result", "ok")
    r = _client(con, tmp_path).get("/settings/health")
    assert "10월 7일 03:00" in r.text and "4.2 MB" in r.text
    assert "백업을 확인해 주세요" not in r.text


def test_health_shows_check_on_failure(con, tmp_path):
    _pref(con, "backup.last_ok_at", LAST.isoformat())
    _pref(con, "backup.last_bytes", "100")
    _pref(con, "backup.last_result", "failed")
    _pref(con, "backup.last_reason", "low_disk")
    r = _client(con, tmp_path).get("/settings/health")
    assert "백업을 확인해 주세요" in r.text and "저장 공간이 부족합니다" in r.text


def test_health_stale_48h_shows_check(con, tmp_path, monkeypatch):
    _pref(con, "backup.last_ok_at", LAST.isoformat())
    _pref(con, "backup.last_result", "ok")
    c = _client(con, tmp_path)
    monkeypatch.setattr(routes_health, "_now", lambda: LAST + timedelta(hours=47))
    assert "백업을 확인해 주세요" not in c.get("/settings/health").text
    monkeypatch.setattr(routes_health, "_now", lambda: LAST + timedelta(hours=49))
    assert "백업을 확인해 주세요" in c.get("/settings/health").text


def test_health_mirror_states(con, tmp_path):
    c = _client(con, tmp_path)
    assert "외장" not in c.get("/settings/health").text.split('id="backup"')[1]
    _pref(con, "backup.mirror_state", "off")
    assert "외장" not in c.get("/settings/health").text.split('id="backup"')[1]
    _pref(con, "backup.mirror_state", "missing")
    assert "외장 백업 위치를 찾을 수 없습니다" in c.get("/settings/health").text
    _pref(con, "backup.mirror_state", "ok")
    assert "외장 백업: 연결됨" in c.get("/settings/health").text


def test_health_never_shows_paths_or_names(con, tmp_path):
    _pref(con, "backup.last_ok_at", LAST.isoformat())
    _pref(con, "backup.last_bytes", "4404019")
    _pref(con, "backup.last_result", "ok")
    from pa.backup.crypt import key_fingerprint
    r = _client(con, tmp_path).get("/settings/health")
    body = r.text.split('id="backup"')[1]
    for bad in (str(tmp_path), "backups", ".pabk", "run.json", "\\", key_fingerprint("K" * 44)):
        assert bad not in body, bad
    assert not re.search(r"2026100\d", body) and not re.search(r"\d{8}T\d{6}Z", r.text)
    assert "core" not in body and "audit_log" not in body


def test_health_shows_retention_note(con, tmp_path):
    r = _client(con, tmp_path).get("/settings/health")
    assert "백업 보존 기간(14일)" in r.text


def test_health_has_no_restore_controls(con, tmp_path):
    r = _client(con, tmp_path).get("/settings/health")
    assert "<button" not in r.text.split('id="backup"')[1] and "<form" not in r.text.split('id="backup"')[1]

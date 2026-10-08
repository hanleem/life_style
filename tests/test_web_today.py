"""홈 '오늘 꼭 할 일' 상자 → /today 상세 화면."""
from conftest import freeze_clock, owner_client
from fastapi.testclient import TestClient

from pa.settings import Settings
from pa.web.app import create_app
from test_web_voice import T_MORNING, TODAY, _briefing, _sec


def _client(con, tmp_path, monkeypatch):
    freeze_clock(monkeypatch, T_MORNING)
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    c = owner_client(create_app(s, con, None))
    return c


def test_home_box_links_to_today_detail(con, tmp_path, monkeypatch):
    _briefing(con, _sec(today_must=[{"text": "샘플 확인", "source": "task", "reasons": ["이번 주 마감"], "date": TODAY}]))
    html = _client(con, tmp_path, monkeypatch).get("/").text
    assert 'href="/today"' in html and "자세히" in html


def test_today_detail_shows_reasons_rest_and_approvals(con, tmp_path, monkeypatch):
    sec = _sec(today_must=[{"text": "샘플 확인", "source": "task", "reasons": ["이번 주 마감", "답 기다리는 사람"], "date": TODAY}],
               rest_count=2, rest_items=[{"text": "나중 일", "source": "task", "reasons": [], "date": None}],
               calendar=[{"time": "10:30", "title": "학부모 상담"}], approvals=3)
    _briefing(con, sec)
    html = _client(con, tmp_path, monkeypatch).get("/today").text
    for want in ("샘플 확인", "이번 주 마감", "답 기다리는 사람", "그 외 2건", "나중 일", "10:30 학부모 상담",
                 "승인 대기 3건"):
        assert want in html, want
    assert "앞의 1건만" in html


def test_today_without_briefing_and_login_required(con, tmp_path, monkeypatch):
    freeze_clock(monkeypatch, T_MORNING)
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    assert TestClient(create_app(s, con, None)).get("/today").status_code in (401, 403)
    assert "오늘 브리핑이 아직 없습니다" in owner_client(create_app(s, con, None)).get("/today").text

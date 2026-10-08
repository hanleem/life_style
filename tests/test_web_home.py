from conftest import owner_client
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from pa.core.calendar_actions import create_calendar_draft
from pa.settings import Settings
from pa.web.app import create_app


def today_kst_iso():
    return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()


@pytest.fixture
def app_client(con):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True)
    c = owner_client(create_app(s, con, None))
    return c


def _briefing(con, sections, for_date=None):
    con.execute("INSERT INTO briefing(id,kind,for_date,body_md,sections_json,created_at) VALUES('b','morning',?,'',?,'t')",
                (for_date or today_kst_iso(), json.dumps(sections, ensure_ascii=False)))


def _sec(**kw):
    base = {"today_must": [], "rest_count": 0, "calendar": [], "approvals": 0}
    return {**base, **kw}


def test_home_requires_login(con):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True)
    r = TestClient(create_app(s, con, None)).get("/", follow_redirects=False)   # 루프백이 아닌 접속
    assert r.status_code == 401 and "아침 브리핑" not in r.text


def test_home_shows_three_and_rest(app_client, con):
    _briefing(con, _sec(today_must=[{"text": "A"}, {"text": "B"}, {"text": "C"}], rest_count=4))
    r = app_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert all(x in html for x in ("A", "B", "C")) and "그 외 4건" in html


def test_home_before_briefing(app_client, con):
    _briefing(con, _sec(), for_date="2000-01-01")
    assert "아침 브리핑 전입니다" in app_client.get("/").text


def test_home_counts_and_calendar_escaped(app_client, con):
    con.execute("INSERT INTO preference(key,value) VALUES('calendar.write_ids','[\"primary\"]')")
    create_calendar_draft(con, {"calendar_id": "primary", "title": "회의", "start": "2026-10-07T05:00:00+00:00",
                                "end": "2026-10-07T06:00:00+00:00"})
    _briefing(con, _sec(calendar=[{"title": "<script>x</script>", "time": "09:00", "all_day": False}]))
    html = app_client.get("/").text
    assert 'href="/approvals"' in html and "승인 대기 1건" in html
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html


def test_approvals_calendar_view(app_client, con):
    con.execute("INSERT INTO preference(key,value) VALUES('calendar.write_ids','[\"primary\"]')")
    create_calendar_draft(con, {"calendar_id": "primary", "title": "상담", "start": "2026-10-07T05:00:00+00:00",
                                "end": "2026-10-07T06:00:00+00:00"})
    html = app_client.get("/approvals").text
    assert "승인하고 캘린더에 추가" in html and "일정 추가: 상담 (10/07 14:00–15:00 KST)" in html and "캘린더" in html


# ---- 이유 배지 ----
def test_home_shows_reason_badge(app_client, con):
    _briefing(con, _sec(today_must=[
        {"text": "자료 보내기", "reasons": ["오늘 마감"], "task_id": "t1", "source": "task"},
        {"text": "직접 할 일", "reasons": ["기한 지남"], "task_id": "t2", "source": "task"}]))
    html = app_client.get("/").text
    assert "오늘 마감" in html and "기한 지남" in html


def test_home_old_cached_sections_still_render(app_client, con):
    _briefing(con, {"today_must": [{"text": "옛항목", "source": "task"}], "rest_count": 1})
    html = app_client.get("/").text
    assert "옛항목" in html and 'data-drop="' not in html and 'data-cal="' not in html


def test_home_caps_at_three(app_client, con):
    _briefing(con, _sec(today_must=[{"text": f"항목{i}", "reasons": []} for i in range(5)], rest_count=0))
    html = app_client.get("/").text
    assert "항목0" in html and "항목2" in html and "항목3" not in html


def test_home_hostile_text_escaped_in_badges(app_client, con):
    _briefing(con, _sec(today_must=[{"text": "<b>x</b>", "reasons": ["<i>y</i>"], "task_id": "t'1"}]))
    html = app_client.get("/").text
    assert "<b>x</b>" not in html and "<i>y</i>" not in html

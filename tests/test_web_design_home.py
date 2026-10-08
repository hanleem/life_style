import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import calendar_draft, owner_client
from design_helpers import TEMPLATES, inline_styles

from pa.settings import Settings
from pa.web.app import create_app

NAMES = ["home", "approvals", "tasks", "outing"]


def _src(n):
    return (TEMPLATES / f"{n}.html").read_text(encoding="utf-8")


def _today():
    return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()


@pytest.fixture
def client(con, tmp_path):
    return owner_client(create_app(Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path), con, None))


@pytest.mark.parametrize("name", NAMES)
def test_no_inline_style(name):
    src = _src(name)
    assert inline_styles(src) == [] and not re.search(r"#[0-9a-fA-F]{3,8}\b", src)


def test_home_mockup_structure(client, con):
    sections = {"today_must": [{"id": "task:1", "text": "A"}, {"id": "task:2", "text": "B"}], "rest_count": 4,
                "calendar": [], "approvals": 1}
    con.execute("INSERT INTO briefing(id,kind,for_date,body_md,sections_json,created_at) VALUES('b','morning',?,'',?,'t')",
                (_today(), json.dumps(sections, ensure_ascii=False)))
    html = client.get("/").text
    assert '<ol class="must">' in html and "그 외 4건" in html
    assert 'href="/approvals"' in html


def test_home_pending_chip(con, client):
    calendar_draft(con)
    assert 'chip chip-a">승인 필요' in client.get("/").text


def test_home_voice_block_before_end_and_no_autoplay():
    src = _src("home")
    assert '{% include "_voice_home.html" %}' in src
    assert "<audio" not in src and "autoplay" not in src


def test_approvals_mockup_structure(con, client):
    calendar_draft(con)
    html = client.get("/approvals").text
    assert '<pre class="draft-body">' in html and "· 검토 중" in html
    assert re.search(r'class="btn-primary"[^>]*>✅ 승인하고 캘린더에 추가', html)
    assert html.count('class="btn-block"') == 1
    assert "승인한 뒤에만 실행됩니다." in html
    assert '<span class="pastrip-right">승인 대기</span>' in html


def test_approvals_empty_state(client):
    html = client.get("/approvals").text
    assert 'card empty' in html and "대기 중인 항목이 없습니다." in html
    assert "승인한 뒤에만 실행됩니다" not in html


def test_approval_wording_sources():
    src = _src("approvals")
    assert "승인하고 캘린더에 추가" in src and "메일" not in src

import re
from pathlib import Path

import pytest
from conftest import calendar_draft, owner_client

from pa.settings import Settings
from pa.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "pa/web/static/app.css"
TEMPLATES = ROOT / "pa/web/templates"

TABS = [("/", "홈"), ("/tasks", "할 일"), ("/meals", "식단"), ("/approvals", "승인"), ("/routine", "생활")]
# 생활 탭 아래 화면은 모두 '생활' 탭을 현재로 표시한다.
LIFE_PAGES = ["/routine/settings", "/outing", "/settings", "/settings/google", "/settings/taste", "/settings/scout",
              "/settings/health"]


def _app(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    return create_app(s, con, None)


@pytest.fixture
def app_client(con, tmp_path):
    return owner_client(_app(con, tmp_path))


def _current(html):
    return re.findall(r'<a [^>]*aria-current="page"[^>]*>(.*?)</a>', html, re.DOTALL)


def test_app_css_has_no_hex_colors():
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", CSS.read_text(encoding="utf-8"))


def test_app_css_touch_targets_and_classes():
    css = CSS.read_text(encoding="utf-8")
    for cls in (".card", ".meta", ".btn", ".btn-primary", ".btn-danger", ".badge", ".tabbar", ".toast"):
        assert cls in css, cls
    assert re.search(r"\.btn\b[^{]*\{[^}]*min-height:\s*var\(--touch\)", css)
    assert re.search(r"\.tabbar a\s*\{[^}]*min-height:\s*var\(--touch\)", css)


def test_base_has_no_inline_style_block_or_hex():
    html = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "<style" not in html and not re.search(r"#[0-9a-fA-F]{3,8}\b", html)
    assert "/static/tokens.css" in html and "/static/app.css" in html


@pytest.mark.parametrize("path,label", TABS)
def test_tabbar_on_tab_pages(app_client, path, label):
    r = app_client.get(path)
    assert r.status_code == 200, path
    html = r.text
    assert html.count('class="tabbar"') == 1, path
    cur = _current(html)
    assert len(cur) == 1 and label in cur[0], (path, cur)
    for _, l in TABS:
        assert f"{l}</span>" in html


@pytest.mark.parametrize("path", LIFE_PAGES)
def test_life_subpages_mark_life_tab(app_client, path):
    r = app_client.get(path)
    assert r.status_code == 200, path
    assert r.text.count('class="tabbar"') == 1 and "<style" not in r.text, path
    cur = _current(r.text)
    assert len(cur) == 1 and "생활" in cur[0], (path, cur)


def test_tab_hrefs(app_client):
    html = app_client.get("/").text
    nav = html[html.index('class="tabbar"'):]
    for href in ("/", "/tasks", "/meals", "/approvals", "/routine"):
        assert f'href="{href}"' in nav


def test_removed_pages_are_gone(app_client):
    for path in ("/lab", "/chat", "/life", "/weekly", "/work", "/discover", "/auth/" + "lab" + "note", "/auth/web",
                 "/settings/chat", "/settings/lessons"):
        assert app_client.get(path).status_code in (404, 405), path


def test_static_served(app_client):
    r = app_client.get("/static/tokens.css")
    assert r.status_code == 200 and "--paper" in r.text
    r = app_client.get("/static/app.css")
    assert r.status_code == 200 and ".tabbar" in r.text


def test_static_needs_no_login(con, tmp_path):
    from fastapi.testclient import TestClient
    c = TestClient(_app(con, tmp_path))    # 소유자가 아닌 접속
    assert c.get("/static/tokens.css").status_code == 200


def test_static_still_behind_tailscale_guard(con):
    s = Settings(session_secret="t" * 32, allowed_tailscale_login="family@example.com")
    c = owner_client(create_app(s, con, None), headers={"x-forwarded-for": "100.64.0.9"})   # 프록시를 거친 접속
    assert c.get("/static/tokens.css").status_code == 403
    ok = c.get("/static/tokens.css", headers={"tailscale-user-login": "family@example.com"})
    assert ok.status_code == 200


def test_approval_button_text():
    src = (TEMPLATES / "approvals.html").read_text(encoding="utf-8")
    assert "승인하고 캘린더에 추가" in src


def test_approvals_badge_and_button_wording(con, tmp_path):
    c = owner_client(_app(con, tmp_path))
    calendar_draft(con)
    html = c.get("/approvals").text
    assert "승인하고 캘린더에 추가" in html
    m = re.search(r'class="badge"[^>]*>\s*(\d+)\s*<', html)
    assert m and m.group(1) == "1"
    assert 'class="badge"' in c.get("/").text


def test_approvals_badge_hidden_when_zero(app_client):
    assert 'class="badge"' not in app_client.get("/approvals").text


def test_no_hex_colors_in_converted_templates():
    for f in ("base.html", "home.html", "approvals.html", "tasks.html"):
        txt = (TEMPLATES / f).read_text(encoding="utf-8")
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", txt), f
        assert "<style" not in txt, f

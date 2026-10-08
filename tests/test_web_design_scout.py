import re

import pytest
from conftest import owner_client
from design_helpers import TEMPLATES, inline_styles

from pa.core import recs
from pa.settings import Settings
from pa.web.app import create_app

SCANNED = ["outing", "taste", "scout_settings", "settings_google", "google_done"]


def _src(n):
    return (TEMPLATES / f"{n}.html").read_text(encoding="utf-8")


@pytest.fixture
def ctx(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, scout_config_path=tmp_path / "none.yaml",
                 data_dir=tmp_path)
    return owner_client(create_app(s, con, None)), con


@pytest.mark.parametrize("name", SCANNED)
def test_no_inline_style_or_hex(name):
    src = _src(name)
    assert inline_styles(src) == [] and not re.search(r"#[0-9a-fA-F]{3,8}\b", src)


def test_google_buttons_are_links_not_nested(con, tmp_path):
    from cryptography.fernet import Fernet
    secret = tmp_path / "client_secret.json"
    secret.write_text('{"installed": {"client_id": "x", "client_secret": "y"}}')
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path,
                 google_token_key=Fernet.generate_key().decode(), google_client_secret_path=secret)
    html = owner_client(create_app(s, con, None)).get("/settings/google").text
    assert not re.search(r"<a[^>]*><button", html)
    for level in ("base", "calwrite"):
        assert f'<a class="btn btn-primary" href="/auth/google/start?level={level}">' in html, level
    assert "account=" not in html and "gmail" not in html.lower()


def test_google_calendar_checkboxes_use_check_row():
    src = _src("settings_google")
    assert src.count('<label class="check-row">') == 2 and "notice-danger" in src


def test_scout_settings_fields(ctx):
    c, _ = ctx
    html = c.get("/settings/scout").text
    for n in ("lo", "hi", "max", "from_region", "vac"):
        assert f'name="{n}"' in html, n
    assert "grant" not in html.lower() and "공고" not in html


def test_taste_icon_buttons_have_labels(ctx):
    c, con = ctx
    recs.ensure_schema(con)
    con.execute("INSERT INTO outing_suggestion(id,for_week,course_json,sources_json,status,created_at) "
                "VALUES('s1','w','{}','[]','proposed','x')")
    rid = recs.record_recommendation(con, domain="outing", item_ref="s1", features={"oneway_bucket": "30-60"},
                                     query_used="q", now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    con.commit()
    c.post("/api/feedback", json={"rec_id": rid, "rating": "like"})
    html = c.get("/settings/taste").text
    assert re.search(r'class="js-pin" aria-label="[^"]+ 고정"', html)
    assert re.search(r'class="js-del" aria-label="[^"]+ 삭제"', html)
    assert "btn-danger" in html


def test_outing_page_has_actions(ctx):
    c, con = ctx
    con.execute("INSERT INTO outing_suggestion(id,for_week,course_json,sources_json,status,created_at) "
                "VALUES('s1','w',?, '[]','proposed','x')", ('{"walk": {"name": "둘레길"}, "food": {"place": "식당"}}',))
    recs.record_recommendation(con, domain="outing", item_ref="s1", features={}, query_used="q",
                               now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    con.commit()
    html = c.get("/outing").text
    assert "둘레길 + 식당" in html and 'data-rate="like"' in html

"""승인 화면의 '보류'는 항목을 삭제한다. 설정 입구."""
from conftest import calendar_draft, owner_client
from fastapi.testclient import TestClient

from pa.core import drafts
from pa.settings import Settings
from pa.web.app import create_app


def _client(con, tmp_path, login=True):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    app = create_app(s, con, None)
    return owner_client(app) if login else TestClient(app)


def _status(con, did):
    return con.execute("SELECT status FROM draft WHERE id=?", (did,)).fetchone()[0]


def test_approvals_page_button_discards_instead_of_just_holding(con, tmp_path):
    calendar_draft(con)
    html = _client(con, tmp_path).get("/approvals").text
    assert "discardDraft(" in html and "/hold" not in html            # 눌러도 반응 없던 '보류' 요청은 더 쓰지 않는다
    assert "삭제" in html


def test_discard_cancels_the_draft(con, tmp_path):
    did = calendar_draft(con)
    r = _client(con, tmp_path).post(f"/api/drafts/{did}/discard")
    assert r.json() == {"ok": True} and _status(con, did) == "cancelled"
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE event='draft.discard'").fetchone()[0] == 1


def test_held_draft_can_be_discarded_and_queued_cannot(con, tmp_path):
    did = calendar_draft(con)
    drafts.set_status(con, did, "held")
    c = _client(con, tmp_path)
    assert c.post(f"/api/drafts/{did}/discard").status_code == 200 and _status(con, did) == "cancelled"
    did2 = calendar_draft(con, "다른 일정", hour=8)
    drafts.set_status(con, did2, "queued")                                # 이미 승인되어 실행 대기 중: 삭제 불가
    assert c.post(f"/api/drafts/{did2}/discard").status_code in (409, 422)
    assert _status(con, did2) == "queued"


def test_discard_requires_the_owner(con, tmp_path):
    did = calendar_draft(con)
    c = _client(con, tmp_path, login=False)
    assert c.post(f"/api/drafts/{did}/discard").status_code in (401, 403)
    assert _status(con, did) == "review"


def test_settings_hub_lists_every_settings_page_and_is_linked_from_the_header(con, tmp_path):
    c = _client(con, tmp_path)
    home = c.get("/").text
    assert 'href="/settings"' in home and "설정" in home
    page = c.get("/settings")
    assert page.status_code == 200
    for path in ("/settings/google", "/settings/voice", "/settings/health", "/settings/taste", "/routine/settings"):
        assert f'href="{path}"' in page.text, path


def test_settings_hub_requires_the_owner(con, tmp_path):
    assert _client(con, tmp_path, login=False).get("/settings").status_code in (401, 403)

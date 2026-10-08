"""가정통신문 화면: 사진 올리기 → 후보 확인 → 할 일 후보 / 일정 초안(승인 필요)."""
import json
import os

import pytest
from conftest import owner_client
from fastapi.testclient import TestClient

from pa.core import drafts, notice, profile
from pa.llm.router import LLMResult
from pa.settings import Settings
from pa.web.app import create_app

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
RAW = {"events": [{"title": "현장체험학습", "date": "2026-10-20", "start_time": "09:00",
                   "items_to_prepare": ["도시락", "물통"], "child": "민준", "note": ""}]}


class FakeRouter:
    tone = "polite"
    honorific = ""

    def __init__(self, data=RAW):
        self.data, self.paths = data, []

    async def run(self, task, system_name, text, schema, *, user_initiated=False, fallback=None, images=()):
        self.paths += list(images)
        return LLMResult(self.data, "ai", self.data is None)


@pytest.fixture
def env(con, tmp_path):
    profile.save(tmp_path, {**profile.defaults(), "members": [
        {"role": "adult", "age": 40, "sex": "f"}, {"role": "child", "age": 10, "sex": "m", "name": "민준"},
        {"role": "child", "age": 8, "sex": "f"}]})
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    router = FakeRouter()
    app = create_app(s, con, router)
    return app, owner_client(app, follow_redirects=False), router, con


def _upload(c, data=JPEG, name="n.jpg", ctype="image/jpeg"):
    return c.post("/notice", files={"photo": (name, data, ctype)})


def test_page_has_a_camera_friendly_upload_form(env):
    _, c, _, _ = env
    r = c.get("/notice")
    assert r.status_code == 200 and 'type="file"' in r.text and 'accept="image/jpeg,image/png,image/webp"' in r.text
    assert 'enctype="multipart/form-data"' in r.text and "가정통신문" in r.text


def test_strangers_cannot_use_it(env):
    app, _, _, _ = env
    s = TestClient(app, base_url="http://localhost:8010", client=("203.0.113.9", 5000), follow_redirects=False)
    assert s.get("/notice").status_code == 401
    assert s.post("/notice", files={"photo": ("a.jpg", JPEG, "image/jpeg")}).status_code == 401
    assert s.post("/api/notice/x/tasks").status_code == 401


def test_upload_reads_the_photo_and_shows_candidates_then_deletes_it(env):
    _, c, router, con = env
    r = _upload(c)
    assert r.status_code == 303 and r.headers["location"].startswith("/notice?r=ok")
    assert router.paths and not os.path.exists(router.paths[0])             # 사진은 처리 뒤 지워진다
    html = c.get("/notice").text
    assert "현장체험학습" in html and "2026-10-20" in html and "도시락" in html and "물통" in html
    assert "민준" in html                                                    # 이름으로 아이를 맞췄다
    assert notice.list_pending(con)[0]["child_index"] == 0


def test_result_messages_are_fixed_texts_selected_by_a_code(env):
    _, c, _, _ = env
    assert "후보를 찾았어요" in c.get("/notice?r=ok&n=2").text
    assert "일정이나 준비물을 찾지 못했어요" in c.get("/notice?r=empty").text
    assert "사진을 읽지 못했어요" in c.get("/notice?r=fail").text
    html = c.get("/notice?r=<script>alert(1)</script>").text
    assert "alert(1)" not in html


def test_bad_uploads_get_friendly_messages_and_never_reach_the_ai(env):
    _, c, router, _ = env
    r = _upload(c, b"%PDF-1.4", "a.pdf", "application/pdf")
    assert r.status_code == 303 and "r=type" in r.headers["location"] and router.paths == []
    big = JPEG + b"\x00" * (notice.MAX_IMAGE_BYTES + 10)
    assert "r=big" in _upload(c, big).headers["location"] and router.paths == []
    assert "r=type" in c.post("/notice").headers["location"]                  # 파일을 안 고르고 누른 경우


def test_ai_failure_says_so_without_saving_anything(env):
    app, c, _, con = env
    app.state.router.data = None
    r = _upload(c)
    assert "r=fail" in r.headers["location"] and notice.list_pending(con) == []


def test_nothing_found_message(env):
    app, c, _, _ = env
    app.state.router.data = {"events": []}
    assert "r=empty" in _upload(c).headers["location"]


def _first(con):
    return notice.list_pending(con)[0]["id"]


def test_add_tasks_makes_candidates_shown_in_the_tasks_flow(env):
    _, c, _, con = env
    _upload(c)
    eid = _first(con)
    r = c.post(f"/api/notice/{eid}/tasks")
    assert r.status_code == 200 and r.json() == {"ok": True, "count": 2}
    assert con.execute("SELECT COUNT(*) FROM task WHERE status='candidate'").fetchone()[0] == 2
    assert "할 일 후보로 담았어요" in c.get("/notice").text
    assert c.post(f"/api/notice/{eid}/tasks").json()["count"] == 2             # 다시 눌러도 그대로
    assert con.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 2


def test_calendar_button_needs_a_connected_calendar(env):
    _, c, _, con = env
    _upload(c)
    eid = _first(con)
    assert "캘린더에 추가" not in c.get("/notice").text                         # 연결 전에는 버튼을 숨긴다
    r = c.post(f"/api/notice/{eid}/calendar")
    assert r.status_code == 409 and "캘린더" in r.json()["detail"]
    con.execute("INSERT OR REPLACE INTO preference(key,value) VALUES('calendar.write_ids','[\"primary\"]')")
    assert "캘린더에 추가" in c.get("/notice").text
    r = c.post(f"/api/notice/{eid}/calendar")
    assert r.status_code == 200 and drafts.get_draft(con, r.json()["draft_id"])["status"] == "review"
    assert "승인" in c.get("/notice").text


def test_calendar_for_an_event_without_a_date_is_refused(env):
    app, c, _, con = env
    app.state.router.data = {"events": [{"title": "알림장 확인", "date": ""}]}
    _upload(c)
    con.execute("INSERT OR REPLACE INTO preference(key,value) VALUES('calendar.write_ids','[\"primary\"]')")
    assert c.post(f"/api/notice/{_first(con)}/calendar").status_code == 400


def test_choose_the_child_and_finish_or_discard(env):
    _, c, _, con = env
    _upload(c)
    eid = _first(con)
    assert c.post(f"/api/notice/{eid}/child", json={"index": 1}).json() == {"ok": True}
    assert notice.get(con, eid)["child_index"] == 1
    assert c.post(f"/api/notice/{eid}/child", json={"index": 7}).status_code == 400
    assert c.post(f"/api/notice/{eid}/child", json={"index": None}).status_code == 200
    assert c.post(f"/api/notice/{eid}/finish").status_code == 200 and notice.list_pending(con) == []
    assert c.post("/api/notice/nope/finish").status_code == 404 and c.post("/api/notice/nope/dismiss").status_code == 404
    _upload(c)
    assert c.post(f"/api/notice/{_first(con)}/dismiss").status_code == 200 and notice.list_pending(con) == []


def test_html_in_extracted_text_is_escaped(env):
    app, c, _, _ = env
    app.state.router.data = {"events": [{"title": "<script>alert(1)</script>", "items_to_prepare": ["<img src=x onerror=alert(1)>"]}]}
    _upload(c)
    html = c.get("/notice").text
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html


def test_settings_and_home_link_to_the_notice_page(env):
    _, c, _, _ = env
    assert "/notice" in c.get("/settings").text and "/kids" in c.get("/settings").text

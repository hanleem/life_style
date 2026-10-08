"""가정통신문 사진 → 일정·할 일 후보. 사진은 처리가 끝나면 지운다."""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import cal_payload

from pa.core import drafts, notice, profile
from pa.core.errors import Forbidden
from pa.llm.router import LLMResult

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=KST)
PROF = {**profile.defaults(), "members": [
    {"role": "adult", "age": 40, "sex": "f"},
    {"role": "child", "age": 10, "sex": "m", "name": "민준"},
    {"role": "child", "age": 8, "sex": "f"}]}
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def _raw(**over):
    ev = {"title": "현장체험학습", "date": "2026-10-20", "start_time": "09:00", "items_to_prepare": ["도시락", "물통", "돗자리"],
          "child": "3학년 2반 민준", "note": "우천 시 연기"}
    ev.update(over)
    return {"events": [ev]}


def test_image_sniffing_by_magic_bytes_not_by_name():
    assert notice.sniff_image(JPEG) == "jpg" and notice.sniff_image(PNG) == "png" and notice.sniff_image(WEBP) == "webp"
    for bad in (b"GIF89a....", b"%PDF-1.4", b"<svg></svg>", b"", b"\xff\xd8", b"MZ\x90\x00", b"RIFF\x00\x00\x00\x00WAVEfmt "):
        assert notice.sniff_image(bad) is None


def test_parse_keeps_valid_fields_and_cleans_the_rest():
    ev = notice.parse(_raw())[0]
    assert ev == {"title": "현장체험학습", "date": "2026-10-20", "start_time": "09:00", "items": ["도시락", "물통", "돗자리"],
                  "child_hint": "3학년 2반 민준", "note": "우천 시 연기"}


@pytest.mark.parametrize("over,key,expect", [
    ({"date": "내일"}, "date", ""), ({"date": "2026-02-30"}, "date", ""), ({"date": "2026-10-20T09:00"}, "date", ""),
    ({"start_time": "9시"}, "start_time", ""), ({"start_time": "25:00"}, "start_time", ""),
    ({"items_to_prepare": "도시락"}, "items", []), ({"items_to_prepare": [1, None, "  ", "물통"]}, "items", ["물통"]),
    ({"child": None}, "child_hint", ""),
])
def test_parse_drops_malformed_values_instead_of_guessing(over, key, expect):
    assert notice.parse(_raw(**over))[0][key] == expect


def test_parse_limits_and_rejects_titleless_or_broken_input():
    long_items = [f"준비물{i}" for i in range(30)]
    ev = notice.parse(_raw(title="가" * 300, items_to_prepare=long_items, note="나" * 500))[0]
    assert len(ev["title"]) <= notice.TITLE_MAX and len(ev["items"]) == notice.MAX_ITEMS and len(ev["note"]) <= notice.NOTE_MAX
    assert notice.parse(_raw(title="  ")) == [] and notice.parse({}) == [] and notice.parse(None) == []
    assert notice.parse({"events": "x"}) == [] and notice.parse({"events": [None, 3, "x"]}) == []
    assert len(notice.parse({"events": [_raw()["events"][0]] * 40})) == notice.MAX_EVENTS


def test_child_is_matched_by_name_only_and_never_guessed():
    assert notice.match_child("3학년 2반 민준", PROF) == 0
    assert notice.match_child("민준이네", PROF) == 0
    assert notice.match_child("3학년 2반", PROF) is None and notice.match_child("", PROF) is None
    assert notice.match_child("둘째", PROF) is None          # 이름이 없는 아이는 사람이 고른다
    assert notice.match_child("민준", {**profile.defaults()}) is None


def test_prompt_has_today_and_child_ages_but_no_names():
    text = notice.prompt(PROF, NOW.date())
    assert "2026-10-08" in text and "10세" in text and "8세" in text and "민준" not in text
    assert "연도" in text


def test_store_list_and_child_choice(con):
    ids = notice.store(con, notice.parse(_raw()), PROF, NOW)
    assert len(ids) == 1
    ev = notice.list_pending(con)[0]
    assert ev["title"] == "현장체험학습" and ev["child_index"] == 0 and ev["items"] == ["도시락", "물통", "돗자리"]
    notice.set_child(con, ids[0], 1)
    assert notice.get(con, ids[0])["child_index"] == 1
    notice.set_child(con, ids[0], None)
    assert notice.get(con, ids[0])["child_index"] is None
    with pytest.raises(ValueError):
        notice.set_child(con, ids[0], 9)
    with pytest.raises(KeyError):
        notice.get(con, "nope")


def test_add_tasks_creates_candidates_due_the_day_before(con):
    eid = notice.store(con, notice.parse(_raw()), PROF, NOW)[0]
    tids = notice.add_tasks(con, eid, NOW, PROF)
    assert len(tids) == 3
    rows = [dict(con.execute("SELECT * FROM task WHERE id=?", (t,)).fetchone()) for t in tids]
    assert {r["status"] for r in rows} == {"candidate"}                    # 확정 전 후보로만
    assert rows[0]["title"] == "[민준(10세)] 도시락 챙기기 · 현장체험학습"
    assert rows[0]["due_at"].startswith("2026-10-19") and rows[0]["due_is_all_day"] == 1
    src = con.execute("SELECT source_type, source_ref FROM task_source WHERE task_id=?", (tids[0],)).fetchone()
    assert src["source_type"] == "memo" and src["source_ref"].startswith(f"notice:{eid}")
    assert notice.add_tasks(con, eid, NOW, PROF) == tids                   # 두 번 눌러도 중복으로 만들지 않는다
    assert con.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 3


def test_event_without_items_becomes_one_check_task_on_the_day(con):
    eid = notice.store(con, notice.parse(_raw(items_to_prepare=[], child="")), PROF, NOW)[0]
    (tid,) = notice.add_tasks(con, eid, NOW, PROF)
    r = con.execute("SELECT title, due_at FROM task WHERE id=?", (tid,)).fetchone()
    assert r["title"] == "현장체험학습 확인" and r["due_at"].startswith("2026-10-20")


def test_event_without_date_makes_tasks_without_due(con):
    eid = notice.store(con, notice.parse(_raw(date="")), PROF, NOW)[0]
    tids = notice.add_tasks(con, eid, NOW, PROF)
    assert all(con.execute("SELECT due_at FROM task WHERE id=?", (t,)).fetchone()[0] is None for t in tids)


def test_add_calendar_makes_an_approval_draft_not_an_event(con):
    con.execute("INSERT OR REPLACE INTO preference(key,value) VALUES('calendar.write_ids','[\"primary\"]')")
    eid = notice.store(con, notice.parse(_raw()), PROF, NOW)[0]
    did = notice.add_calendar(con, eid, PROF)
    assert drafts.get_draft(con, did)["status"] == "review"                 # 승인 전에는 아무것도 쓰지 않는다
    p = json.loads(drafts.current_version(con, did)["payload_json"])
    assert p["title"] == "[민준(10세)] 현장체험학습" and p["start"].startswith("2026-10-20T09:00") and p["end"].startswith("2026-10-20T10:00")
    assert p["calendar_id"] == "primary"
    assert notice.add_calendar(con, eid, PROF) == did                        # 중복 초안을 만들지 않는다


def test_calendar_without_time_defaults_to_nine_and_without_date_is_refused(con):
    con.execute("INSERT OR REPLACE INTO preference(key,value) VALUES('calendar.write_ids','[\"primary\"]')")
    e1 = notice.store(con, notice.parse(_raw(start_time="")), PROF, NOW)[0]
    p = json.loads(drafts.current_version(con, notice.add_calendar(con, e1, PROF))["payload_json"])
    assert p["start"].startswith("2026-10-20T09:00")
    e2 = notice.store(con, notice.parse(_raw(date="")), PROF, NOW)[0]
    with pytest.raises(ValueError):
        notice.add_calendar(con, e2, PROF)


def test_calendar_needs_a_writable_calendar(con):
    eid = notice.store(con, notice.parse(_raw()), PROF, NOW)[0]
    assert notice.calendar_ready(con) is False
    with pytest.raises(Forbidden):
        notice.add_calendar(con, eid, PROF)
    con.execute("INSERT OR REPLACE INTO preference(key,value) VALUES('calendar.write_ids','[\"family@group.v.calendar.google.com\"]')")
    assert notice.calendar_ready(con) is True
    p = json.loads(drafts.current_version(con, notice.add_calendar(con, eid, PROF))["payload_json"])
    assert p["calendar_id"] == "family@group.v.calendar.google.com"                  # primary 가 없으면 허용된 첫 캘린더


def test_event_leaves_the_pending_list_when_done_or_dismissed(con):
    a, b, c = (notice.store(con, notice.parse(_raw(title=t)), PROF, NOW)[0] for t in ("가", "나", "다"))
    notice.finish(con, a)
    notice.dismiss(con, b)
    assert [e["title"] for e in notice.list_pending(con)] == ["다"]
    assert notice.get(con, a)["status"] == "added" and notice.get(con, b)["status"] == "dismissed"
    assert notice.dismiss(con, "nope") is False


class FakeRouter:
    def __init__(self, data=None, exc=None):
        self.data, self.exc, self.seen = data, exc, {}

    async def run(self, task, system_name, text, schema, *, user_initiated=False, fallback=None, images=()):
        assert task == "notice_extract" and system_name == "notice" and user_initiated is True
        path = images[0]
        self.seen = {"path": path, "existed": os.path.exists(path), "size": os.path.getsize(path), "text": text,
                     "ext": os.path.splitext(path)[1]}
        if self.exc:
            raise self.exc
        return LLMResult(self.data, "ai", self.data is None)


async def test_extract_passes_the_photo_then_deletes_it(con):
    r = FakeRouter(_raw())
    ids = await notice.extract(con, r, JPEG, PROF, NOW)
    assert len(ids) == 1 and r.seen["existed"] and r.seen["ext"] == ".jpg" and r.seen["size"] == len(JPEG)
    assert not os.path.exists(r.seen["path"]) and not os.path.exists(os.path.dirname(r.seen["path"]))
    assert "민준" not in r.seen["text"]


async def test_photo_is_deleted_even_when_the_ai_fails(con):
    r = FakeRouter(exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await notice.extract(con, r, PNG, PROF, NOW)
    assert not os.path.exists(r.seen["path"])
    r2 = FakeRouter(None)                                                   # AI 가 못 읽은 경우(degraded)
    assert await notice.extract(con, r2, PNG, PROF, NOW) is None
    assert not os.path.exists(r2.seen["path"])


async def test_extract_refuses_non_images_and_huge_files_before_calling_the_ai(con):
    r = FakeRouter(_raw())
    with pytest.raises(ValueError):
        await notice.extract(con, r, b"%PDF-1.4 ...", PROF, NOW)
    with pytest.raises(ValueError):
        await notice.extract(con, r, JPEG + b"\x00" * notice.MAX_IMAGE_BYTES, PROF, NOW)
    assert r.seen == {}


async def test_nothing_readable_gives_an_empty_result_not_an_error(con):
    ids = await notice.extract(con, FakeRouter({"events": []}), JPEG, PROF, NOW)
    assert ids == [] and notice.list_pending(con) == []

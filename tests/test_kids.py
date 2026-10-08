"""아이 루틴: 등교·하교·학원 요일·시간과 준비물, 하루 전 저녁 알림."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from pa.core import kids, notify, profile
from pa.rules import load_rules

KST = ZoneInfo("Asia/Seoul")
PROF = {**profile.defaults(), "members": [
    {"role": "adult", "age": 40, "sex": "f"},
    {"role": "child", "age": 10, "sex": "m", "name": "민준"},
    {"role": "child", "age": 8, "sex": "f"}]}
TUE = date(2026, 10, 13)
WED = date(2026, 10, 14)


def _raw(**over):
    base = {"prep_time": "20:00", "children": [
        {"school_days": ["mon", "tue", "wed", "thu", "fri"], "school_start": "08:40", "school_end": "13:30",
         "classes": [{"name": "영어학원", "days": ["tue", "thu"], "start": "16:00", "bring": ["영어 책가방", "단어장"]},
                     {"name": "태권도", "days": ["wed"], "start": "17:00", "bring": ["도복"]}]},
        {"school_days": ["mon", "tue", "wed", "thu", "fri"], "school_start": "09:00", "school_end": "12:40", "classes": []}]}
    base.update(over)
    return base


def test_defaults_are_empty_and_valid():
    d = kids.defaults()
    assert d["prep_time"] == "20:00" and d["children"] == []
    assert kids.normalize(d) == d


def test_normalize_accepts_a_full_schedule():
    n = kids.normalize(_raw())
    assert n["children"][0]["classes"][0] == {"name": "영어학원", "days": ["tue", "thu"], "start": "16:00", "bring": ["영어 책가방", "단어장"]}
    assert n["children"][1]["school_end"] == "12:40"


@pytest.mark.parametrize("bad", [
    {"prep_time": "25:00"}, {"prep_time": "abc"}, {"children": "x"},
    {"children": [{"school_days": ["mon", "funday"]}]},
    {"children": [{"school_start": "8시"}]},
    {"children": [{"classes": [{"name": "a", "days": [], "start": "16:00"}]}]},        # 요일이 없는 학원
    {"children": [{"classes": [{"name": "a", "days": ["tue"], "start": "99:00"}]}]},
    {"children": [{}] * (kids.MAX_KIDS + 1)},
])
def test_invalid_schedule_is_rejected_with_an_easy_message(bad):
    with pytest.raises(kids.KidsError):
        kids.normalize(_raw(**bad))


def test_blank_class_rows_are_dropped_and_bring_is_split_and_capped():
    raw = _raw(children=[{"classes": [{"name": "", "days": [], "start": ""},
                                      {"name": "미술", "days": ["fri"], "start": "", "bring": "붓, 팔레트,, " + ",".join("x%d" % i for i in range(20))}]}])
    c = kids.normalize(raw)["children"][0]
    assert [k["name"] for k in c["classes"]] == ["미술"] and c["classes"][0]["bring"][:2] == ["붓", "팔레트"]
    assert len(c["classes"][0]["bring"]) == kids.MAX_BRING and c["school_days"] == ["mon", "tue", "wed", "thu", "fri"]


def test_html_in_names_is_kept_as_plain_text_but_trimmed():
    n = kids.normalize(_raw(children=[{"classes": [{"name": " <b>수학</b> " + "가" * 50, "days": ["mon"], "start": ""}]}]))
    assert len(n["children"][0]["classes"][0]["name"]) <= kids.NAME_MAX


def test_save_and_load_roundtrip(con):
    assert kids.load(con) == kids.defaults()
    saved = kids.save(con, _raw())
    assert kids.load(con) == saved
    kids.save(con, _raw(prep_time="19:30"))
    assert kids.prep_time(con) == time(19, 30)


def test_broken_stored_value_falls_back_to_defaults(con):
    con.execute("INSERT OR REPLACE INTO preference(key,value) VALUES('kids.routine','{{{')")
    assert kids.load(con) == kids.defaults() and kids.prep_time(con) == time(20, 0)


def test_child_labels_use_names_or_birth_order():
    assert kids.child_labels(PROF) == ["민준(10세)", "둘째(8세)"]
    assert kids.child_labels({**profile.defaults()}) == []


def test_tomorrow_items_come_from_that_weekdays_classes(con):
    kids.save(con, _raw())
    plan = kids.day_plan(con, TUE, PROF)              # 화요일: 영어학원
    assert [(i["child"], i["text"]) for i in plan["items"]] == [("민준(10세)", "영어 책가방"), ("민준(10세)", "단어장")]
    assert all("영어학원" in i["why"] for i in plan["items"])
    assert [i["text"] for i in kids.day_plan(con, WED, PROF)["items"]] == ["도복"]
    assert kids.day_plan(con, date(2026, 10, 12), PROF)["items"] == []          # 월요일은 없음


def test_day_plan_lists_school_and_class_times(con):
    kids.save(con, _raw())
    lines = kids.day_plan(con, TUE, PROF)["lines"]
    assert "민준(10세) · 등교 08:40 · 하교 13:30" in lines and "민준(10세) · 영어학원 16:00" in lines
    assert "둘째(8세) · 등교 09:00 · 하교 12:40" in lines
    assert not any("등교" in l for l in kids.day_plan(con, date(2026, 10, 17), PROF)["lines"])     # 토요일은 등교 없음


def test_notice_events_for_the_day_are_included_unless_dismissed(con):
    con.execute("INSERT INTO notice_event(id,created_at,title,event_date,items_json,child_index,status) VALUES('n1','t','현장체험학습','2026-10-14','[\"도시락\",\"물통\"]',1,'new')")
    con.execute("INSERT INTO notice_event(id,created_at,title,event_date,items_json,status) VALUES('n2','t','독서 행사','2026-10-14','[]','dismissed')")
    con.execute("INSERT INTO notice_event(id,created_at,title,event_date,items_json,status) VALUES('n3','t','알림장 확인','2026-10-14','[]','new')")
    items = kids.day_plan(con, WED, PROF)["items"]
    texts = [(i["child"], i["text"], i["why"]) for i in items]
    assert ("둘째(8세)", "도시락", "현장체험학습") in texts and ("둘째(8세)", "물통", "현장체험학습") in texts
    assert ("", "알림장 확인", "가정통신문") in texts and not any("독서" in t[1] + t[2] for t in texts)


def _at(h, m=0):
    return datetime(2026, 10, 13, h, m, tzinfo=KST)


async def test_prep_job_notifies_once_with_the_count_only(con):
    kids.save(con, _raw())
    rules = load_rules()
    await kids.run_prep_job(con, _at(20), rules, PROF)         # 화요일 저녁 → 내일(수) 도복 1개
    await kids.run_prep_job(con, _at(20, 5), rules, PROF)
    rows = notify.unread(con, 50)
    assert [r["title"] for r in rows] == ["내일 준비물이 있어요"] and rows[0]["href"] == "/kids"
    assert "도복" not in rows[0]["title"] and "1" not in rows[0]["title"]


async def test_prep_job_is_silent_when_there_is_nothing_to_bring(con):
    kids.save(con, _raw())
    await kids.run_prep_job(con, datetime(2026, 10, 11, 20, tzinfo=KST), load_rules(), PROF)    # 일요일 저녁 → 월요일 없음
    assert notify.unread(con, 50) == []


async def test_prep_job_respects_quiet_hours(con):
    kids.save(con, _raw())
    await kids.run_prep_job(con, _at(22), load_rules(), PROF)      # 마감(21:30) 이후면 보류
    assert notify.unread(con, 50) == []
    assert con.execute("SELECT COUNT(*) FROM held_notify WHERE kind='kids_prep'").fetchone()[0] == 1


def test_scheduler_runs_the_prep_job_at_the_chosen_time(con):
    from pa.scheduler import due_jobs
    kids.save(con, _raw(prep_time="19:30"))
    rules = load_rules()
    assert "kids_prep" in due_jobs(con, datetime(2026, 10, 13, 19, 31, tzinfo=KST), rules)
    assert "kids_prep" not in due_jobs(con, datetime(2026, 10, 13, 20, 31, tzinfo=KST), rules)    # 유예 시간 지남
    assert "kids_prep" not in due_jobs(con, datetime(2026, 10, 13, 19, 0, tzinfo=KST), rules)


async def test_run_due_jobs_dispatches_the_prep_job(con):
    from pa.core.briefing import run_due_jobs
    kids.save(con, _raw())
    ran = await run_due_jobs(con, _at(20, 1), load_rules(), None, None)
    assert "kids_prep" in ran and notify.unread(con, 5)[0]["title"].startswith("내일 준비물")

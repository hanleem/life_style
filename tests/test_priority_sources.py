"""출처 수집기: 링크·병합·실패 격리."""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pa.core import priority, priority_sources as ps

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 6, 8, 30, tzinfo=KST)
ISO = "2026-10-05T01:00:00+00:00"


def task(con, tid, title="일", status="open", due=None, ts=()):
    con.execute("INSERT INTO task(id,title,bucket,due_at,status,created_at,updated_at) "
                "VALUES(?,?,'check_soon',?,?,?,?)", (tid, title, due, status, ISO, ISO))
    for i, (typ, ref) in enumerate(ts):
        con.execute("INSERT INTO task_source(id,task_id,source_type,source_ref,observed_at) VALUES(?,?,?,?,'x')",
                    (f"{tid}s{i}", tid, typ, ref))


def col(con, **kw):
    kw.setdefault("events", [])
    return ps.collect(con, NOW, **kw)


def by_key(items):
    return {i.key: i for i in items}


def test_open_task_links_include_all_sources(con):
    task(con, "t1", ts=[("memo", "M1"), ("calendar_event", "E7")])
    it = by_key(col(con))["task:t1"]
    assert {"memo:M1", "calendar_event:E7", "task:t1"} <= it.links


def test_task_item_carries_due_and_task_id(con):
    task(con, "t1", title="준비물 챙기기", due="2026-10-06T03:00:00+00:00")
    it = by_key(col(con))["task:t1"]
    assert it.source == "task" and it.task_id == "t1" and it.text == "준비물 챙기기"
    assert it.due_at == datetime.fromisoformat("2026-10-06T03:00:00+00:00")


def test_task_and_event_sharing_a_link_merge(con):
    task(con, "t1", due="2026-10-06T03:00:00+00:00", ts=[("calendar_event", "e1")])
    ev = [{"id": "e1", "title": "학부모 상담", "all_day": False,
           "start": "2026-10-06T15:00:00+09:00", "end": "2026-10-06T16:00:00+09:00"}]
    r = priority.rank(col(con, events=ev), NOW, cap=5, mode="morning")
    assert len(r.all) == 1 and r.all[0]["source"] == "task"


def test_calendar_only_timed_today_events(con):
    ev = [{"id": "e1", "title": "회의", "all_day": False, "start": "2026-10-06T15:00:00+09:00", "end": "2026-10-06T16:00:00+09:00"},
          {"id": "e2", "title": "종일", "all_day": True, "start": "2026-10-06T00:00:00+09:00", "end": "2026-10-07T00:00:00+09:00"},
          {"id": "e3", "title": "내일", "all_day": False, "start": "2026-10-07T15:00:00+09:00", "end": "2026-10-07T16:00:00+09:00"}]
    assert set(by_key(col(con, events=ev))) == {"event:e1"}


def test_approvals_item_only_for_old_calendar_drafts(con):
    def d(i, at):
        con.execute("INSERT INTO draft(id,target_type,status,created_by,created_at,updated_at) VALUES(?,'calendar_event','review','a',?,?)",
                    (i, at, at))
    d("new", (NOW - timedelta(minutes=30)).isoformat())
    assert "approvals" not in by_key(col(con))
    d("old", (NOW - timedelta(hours=3)).isoformat())
    it = by_key(col(con))["approvals"]
    assert it.source == "approvals" and it.text == "승인 대기 1건"


def test_one_collector_failure_isolated(con, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("비밀 본문")
    monkeypatch.setattr(ps, "_approval_items", boom)
    task(con, "t1")
    with caplog.at_level(logging.WARNING):
        items = col(con)
    assert "task:t1" in by_key(items)
    assert "RuntimeError" in caplog.text and "비밀 본문" not in caplog.text


def test_no_database_skips_db_sources():
    ev = [{"id": "e1", "title": "회의", "all_day": False, "start": "2026-10-06T15:00:00+09:00", "end": "2026-10-06T16:00:00+09:00"}]
    assert set(by_key(ps.collect(None, NOW, events=ev))) == {"event:e1"}


def test_module_isolated_from_private():
    src = open(ps.__file__, encoding="utf-8").read()
    assert "import pa.private" not in src and "from pa.private" not in src and "from pa.agents" not in src


# ---- 출처별 제외 규칙 (이름 = test_drop_<출처>_<규칙>, DROP_RULES 와 1:1) ----

def test_drop_calendar_ended(con):
    ev = [{"id": "past", "title": "끝남", "all_day": False, "start": "2026-10-06T07:00:00+09:00", "end": "2026-10-06T08:00:00+09:00"},
          {"id": "now", "title": "진행 중", "all_day": False, "start": "2026-10-06T08:00:00+09:00", "end": "2026-10-06T09:00:00+09:00"},
          {"id": "later", "title": "나중", "all_day": False, "start": "2026-10-06T15:00:00+09:00", "end": "2026-10-06T16:00:00+09:00"}]
    assert set(by_key(col(con, events=ev))) == {"event:now", "event:later"}


def test_drop_calendar_cancelled(con):
    ev = [{"id": "x", "title": "취소", "all_day": False, "status": "cancelled",
           "start": "2026-10-06T15:00:00+09:00", "end": "2026-10-06T16:00:00+09:00"}]
    assert not col(con, events=ev)


def test_drop_calendar_all_day_from_ranking_only(con):
    ev = [{"id": "d", "title": "종일", "all_day": True, "start": "2026-10-06T00:00:00+09:00", "end": "2026-10-07T00:00:00+09:00"}]
    assert not col(con, events=ev)       # 순위 목록에서만 뺀다 — 일정 섹션은 events 를 그대로 쓴다(수집기는 events 를 바꾸지 않음)
    assert ev[0]["all_day"] is True


def test_drop_task_done_and_dropped(con):
    task(con, "d1", status="done")
    task(con, "d2", status="dropped")
    task(con, "o1")
    assert set(by_key(col(con))) == {"task:o1"}


def test_drop_task_candidate_count_only(con):
    task(con, "c1", status="candidate")
    assert not col(con)
    assert ps.candidate_task_count(con) == 1


def test_drop_approvals_young_or_non_calendar(con):
    con.execute("INSERT INTO draft(id,target_type,status,created_by,created_at,updated_at) VALUES('d0','calendar_event','review','a',?,?)",
                ((NOW - timedelta(minutes=30)).isoformat(),) * 2)
    assert not col(con)


def test_drop_rules_table_matches_tests():
    import re
    src = open(__file__, encoding="utf-8").read()
    names = set(re.findall(r"^def (test_drop_\w+)\(", src, re.M))
    expected = {f"test_drop_{s}_{r}" for s, rules in ps.DROP_RULES.items() for r in rules}
    assert expected and expected <= names
    # 거꾸로: 표에 없는 drop 테스트가 없다
    assert names - {"test_drop_rules_table_matches_tests"} <= expected

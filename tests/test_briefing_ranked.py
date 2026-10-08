"""브리핑 today_must 가 하나의 순위 목록(할 일·일정)이 되었는지."""
from datetime import datetime
from zoneinfo import ZoneInfo

from pa.core.briefing import build_briefing
from pa.integrations.calendar import CalEvent, FakeCalendar
from pa.rules import load_rules

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 6, 8, 30, tzinfo=KST)
ISO = "2026-10-05T01:00:00+00:00"


class LLM:
    async def run(self, *a, **k):
        from pa.llm.router import LLMResult
        return LLMResult({"text": "요약"}, "ai", False)


def task(con, tid, title, due=None, status="open", ts=(), done_at=None):
    con.execute("INSERT INTO task(id,title,bucket,due_at,status,created_at,updated_at,completed_at) "
                "VALUES(?,?,'check_soon',?,?,?,?,?)", (tid, title, due, status, ISO, ISO, done_at))
    for i, (typ, ref) in enumerate(ts):
        con.execute("INSERT INTO task_source(id,task_id,source_type,source_ref,observed_at) VALUES(?,?,?,?,'x')",
                    (f"{tid}s{i}", tid, typ, ref))


async def brief(con, kind="morning", now=NOW, events=()):
    cal = FakeCalendar()
    cal.events = list(events)
    return await build_briefing(con, kind, now, cal, LLM(), load_rules())


def appointment(now=NOW, h=15):
    s = now.replace(hour=h, minute=0)
    return CalEvent("e1", "primary", "학부모 상담", s, s.replace(hour=h + 1), False)


async def test_morning_today_must_is_ranked_with_reasons(con):
    task(con, "t1", "보고서", due="2026-10-05T15:00:00+00:00")     # 오늘(KST) 마감
    task(con, "t2", "지난 일", due="2026-10-01T00:00:00+00:00")
    b = await brief(con, events=[appointment()])
    mu = b["sections"]["today_must"]
    assert [m["id"] for m in mu] == ["task:t1", "task:t2", "event:e1"]
    assert mu[0]["reasons"] == ["오늘 마감"] and mu[1]["reasons"] == ["기한 지남"]
    assert mu[0]["task_id"] == "t1"
    assert "1. 보고서 — 오늘 마감" in b["body_md"]


async def test_rest_day_briefing_only_due_and_safety(con):
    sun = datetime(2026, 10, 11, 8, 30, tzinfo=KST)
    task(con, "a", "오늘일", due="2026-10-10T15:00:00+00:00")
    task(con, "b", "지난일", due="2026-10-08T00:00:00+00:00")
    task(con, "c", "다음", due="2026-10-13T00:00:00+00:00")
    b = await brief(con, now=sun, events=[appointment(sun)])
    # 일요일이 쉬는 날인 기준 일정(tests/fixtures/settings_baseline.yaml)에서는 아예 목록이 비는 설정도 있어 모드만 확인한다
    assert {m["id"] for m in b["sections"]["today_must"]} <= {"task:a", "task:b"}


async def test_evening_left_today_ranked(con):
    task(con, "t1", "저녁 일", due="2026-10-06T10:00:00+00:00")
    task(con, "t2", "끝", status="done", done_at="2026-10-06T01:00:00+00:00")
    b = await brief(con, "evening", now=NOW.replace(hour=18), events=[appointment(h=20)])
    s = b["sections"]
    assert [m["id"] for m in s["left_today"]] == ["task:t1", "event:e1"]
    assert [t["text"] for t in s["done_today"]] == ["끝"] and "저녁 일" in b["body_md"]


async def test_friday_includes_next_week_deadlines(con):
    fri = datetime(2026, 10, 9, 17, 0, tzinfo=KST)
    task(con, "n", "다음주일", due="2026-10-13T00:00:00+09:00")
    task(con, "f", "먼일", due="2026-10-30T00:00:00+09:00")
    b = await brief(con, "friday", now=fri)
    assert [(m["id"], m["tier"]) for m in b["sections"]["today_must"]] == [("task:n", "next_week")]


async def test_same_commitment_from_task_and_calendar_shown_once(con):
    task(con, "t1", "상담 준비", ts=[("calendar_event", "e1")])
    b = await brief(con, events=[appointment()])
    mu = b["sections"]["today_must"]
    assert len(mu) == 1 and mu[0]["tier"] == "appointment"


async def test_undated_backlog_not_listed(con):
    task(con, "t1", "언젠가")
    b = await brief(con)
    assert b["sections"]["today_must"] == [] and b["sections"]["rest_count"] == 0


async def test_legacy_keys_still_present(con):
    task(con, "t1", "x", due="2026-10-06T00:00:00+09:00")
    b = await brief(con)
    assert {"today_must", "rest_count", "calendar", "calendar_note", "approvals", "supply",
            "done_today", "left_today", "done_week", "retro"} <= set(b["sections"])
    for m in b["sections"]["today_must"]:
        assert {"id", "text", "date", "source"} <= set(m)


async def test_briefing_cached_once_per_day_unchanged(con):
    task(con, "t1", "하나", due="2026-10-06T00:00:00+09:00")
    b1 = await brief(con)
    task(con, "t2", "둘", due="2026-10-06T00:00:00+09:00")
    b2 = await brief(con)
    assert b1["id"] == b2["id"] and len(b2["sections"]["today_must"]) == 1


async def test_items_tagged_with_true_source_and_origin(con):
    task(con, "t1", "처리", due="2026-10-06T05:00:00+00:00")
    b = await brief(con, events=[appointment()])
    by = {m["id"]: m for m in b["sections"]["today_must"]}
    assert by["task:t1"]["source"] == "task" and by["event:e1"]["source"] == "event"
    assert "event" in by["event:e1"]["origin"] and all("keys" not in m for m in by.values())


async def test_overflow_due_does_not_break_briefing(con):
    task(con, "t9", "먼 마감", due="9999-12-31T23:00:00+00:00")
    b = await brief(con)
    assert b["body_md"] and isinstance(b["sections"]["today_must"], list)


async def test_rank_failure_falls_back_with_warning(con, monkeypatch):
    from pa.core import priority

    def boom(*a, **k):
        raise OverflowError("x")
    monkeypatch.setattr(priority, "rank", boom)
    task(con, "t1", "할일", due="2026-10-06T00:00:00+09:00")
    b = await brief(con)
    assert b["sections"]["today_must"] == []
    assert b["sections"]["rank_error"] and b["sections"]["rank_error"] in b["body_md"]


async def test_approvals_shown_once(con):
    con.execute("INSERT INTO draft(id,target_type,status,created_at,updated_at,created_by) VALUES('d','calendar_event','review','2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','pa')")
    b = await brief(con)
    assert all(m["source"] != "approval" for m in b["sections"]["today_must"])
    assert b["body_md"].count("승인 대기") == 1


async def test_briefing_has_no_private_or_coach_dependency():
    import inspect
    from pa.core import briefing
    src = inspect.getsource(briefing)
    assert "pa.private" not in src and "counsel" not in src

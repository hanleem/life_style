from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pa.core import notify, task_ops
from pa.core.briefing import build_briefing, notify_gated, run_due_jobs
from pa.integrations.calendar import CalEvent, FakeCalendar
from pa.rules import load_rules

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 6, 8, 30, tzinfo=KST)


class LLM:
    calls = 0

    async def run(self, *a, **k):
        LLM.calls += 1
        from pa.llm.router import LLMResult
        return LLMResult({"text": "요약"}, "ai", False)


def _task(con, title, due=None, done=False):
    tid = task_ops.add(con, title, NOW, due_at=due, all_day=due is not None)
    if done:
        task_ops.complete(con, tid, NOW)
    return tid


async def test_sections_and_cap(con):
    for i in range(5):
        _task(con, f"할일{i}", "2026-10-06T00:00:00+09:00")
    cal = FakeCalendar()
    cal.events = [CalEvent("e", "primary", "학교 행사", NOW + timedelta(hours=2), NOW + timedelta(hours=3), False)]
    b = await build_briefing(con, "morning", NOW, cal, LLM(), load_rules())
    s = b["sections"]
    assert len(s["today_must"]) == 3 and s["rest_count"] == 3   # 순위 목록 기준: 할 일 5 + 10:30 일정 1
    assert "학교 행사" in b["body_md"] and "10:30" in b["body_md"]


async def test_same_day_not_rebuilt(con):
    args = (FakeCalendar(), LLM(), load_rules())
    a = await build_briefing(con, "morning", NOW, *args)
    b = await build_briefing(con, "morning", NOW + timedelta(hours=1), *args)
    assert a["id"] == b["id"]
    assert con.execute("SELECT COUNT(*) FROM briefing").fetchone()[0] == 1


async def test_calendar_none_and_llm_failure(con):
    class BadLLM:
        async def run(self, *a, **k):
            raise RuntimeError

    b = await build_briefing(con, "morning", NOW, None, BadLLM(), load_rules())
    assert "캘린더 미연결" in b["body_md"]


async def test_overdue_and_approvals(con):
    _task(con, "지난일", "2026-10-01T00:00:00+09:00")
    b = await build_briefing(con, "morning", NOW, FakeCalendar(), LLM(), load_rules())
    assert [t["text"] for t in b["sections"]["today_must"]] == ["지난일"]
    assert b["sections"]["approvals"] == 0


async def test_evening_and_friday(con):
    _task(con, "끝낸일", "2026-10-06T00:00:00+09:00", done=True)
    _task(con, "남은일", "2026-10-06T00:00:00+09:00")
    e = await build_briefing(con, "evening", NOW, FakeCalendar(), LLM(), load_rules())
    assert "남은일" in e["body_md"] and "끝낸일" in e["body_md"] and "회고" in e["body_md"]
    f = await build_briefing(con, "friday", datetime(2026, 10, 9, 17, 0, tzinfo=KST), FakeCalendar(), LLM(), load_rules())
    assert f["kind"] == "friday" and "다음 주" in f["body_md"]


async def test_after_work_held_then_bundled_next_morning(con):
    R = load_rules()
    r1 = await notify_gated(con, "decision_needed", 1, "d:1", datetime(2026, 10, 6, 23, 0, tzinfo=KST), R)
    r2 = await notify_gated(con, "decision_needed", 2, "d:2", datetime(2026, 10, 6, 23, 30, tzinfo=KST), R)
    assert (r1, r2) == ("hold", "hold") and notify.unread(con) == []
    await run_due_jobs(con, datetime(2026, 10, 7, 8, 31, tzinfo=KST), R, FakeCalendar(), LLM())
    assert [n["title"] for n in notify.unread(con) if n["kind"] == "decision_needed"] == ["결정이 필요한 일 3건"]
    assert con.execute("SELECT COUNT(*) FROM held_notify WHERE released_at IS NULL").fetchone()[0] == 0


async def test_restart_morning_once(con):
    R = load_rules()
    t = datetime(2026, 10, 6, 8, 31, tzinfo=KST)
    await run_due_jobs(con, t, R, FakeCalendar(), LLM())
    await run_due_jobs(con, t + timedelta(minutes=2), R, FakeCalendar(), LLM())
    assert sum(1 for n in notify.unread(con) if n["kind"] == "briefing_morning") == 1


async def test_deliver_returns_deliver_and_sends(con):
    R = load_rules()
    r = await notify_gated(con, "decision_needed", 1, "d:x", datetime(2026, 10, 6, 10, 0, tzinfo=KST), R)
    assert r == "deliver" and len(notify.unread(con)) == 1


async def test_failing_calendar_does_not_stop_others(con):
    R = load_rules()
    con.execute("INSERT INTO held_notify(id,kind,count,dedupe_key,held_at) VALUES('h','decision_needed',2,'d:h','2026-10-06T10:00:00+00:00')")

    class BadCal:
        async def events_between(self, *a):
            raise RuntimeError

    done = await run_due_jobs(con, datetime(2026, 10, 7, 8, 31, tzinfo=KST), R, BadCal(), LLM())
    assert "deliver_held" in done


# --- R3: 식사 공급 섹션 ---
from datetime import date as _date, timezone as _tz

from pa.core import supply as _supply

_N0 = datetime(2026, 10, 5, 8, 0, tzinfo=KST)


def _item(con, **kw):
    kw.setdefault("label", "샐러드 정기배송")
    kw.setdefault("meals_per_workday", 2)
    return _supply.add_item(con, now=_N0, **kw)


async def test_supply_section_due_and_cutoff(con):
    _item(con, remaining=4, order_cutoff="19:00")
    b = await build_briefing(con, "morning", NOW, FakeCalendar(), LLM(), load_rules())
    assert "## 식사 공급" in b["body_md"]
    assert "예상 남은 끼니 4" in b["body_md"]
    assert "이번 주 주문 확인" in b["body_md"] and "(오늘 19:00 전)" in b["body_md"]
    assert b["sections"]["supply"]["items"][0]["due"] is True


async def test_supply_due_even_when_already_reminded(con):
    iid = _item(con, remaining=4)
    _supply.mark_reminded(con, [iid], now=_N0)   # 08:25 job already marked it
    b = await build_briefing(con, "morning", NOW, FakeCalendar(), LLM(), load_rules())
    assert b["sections"]["supply"]["items"][0]["due"] is True and "이번 주 주문 확인" in b["body_md"]


async def test_supply_not_due_when_plenty(con):
    _item(con, remaining=20)
    b = await build_briefing(con, "morning", NOW, FakeCalendar(), LLM(), load_rules())
    assert "예상 남은 끼니 20" in b["body_md"] and "이번 주 주문 확인" not in b["body_md"]


async def test_no_supply_section_without_items_or_in_evening(con):
    b = await build_briefing(con, "morning", NOW, FakeCalendar(), LLM(), load_rules())
    assert "## 식사 공급" not in b["body_md"] and b["sections"]["supply"] is None
    _item(con, remaining=4)
    e = await build_briefing(con, "evening", NOW, FakeCalendar(), LLM(), load_rules())
    assert "## 식사 공급" not in e["body_md"]


async def test_supply_section_failure_isolated(con, monkeypatch):
    _item(con, remaining=4)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(_supply, "list_items", boom)
    b = await build_briefing(con, "morning", NOW, FakeCalendar(), LLM(), load_rules())
    assert "## 식사 공급" not in b["body_md"] and "## 승인 대기" in b["body_md"]


# --- R3 fix round 1: due respects pause / snooze / weekday ---
async def _due(con, now=NOW):
    b = await build_briefing(con, "morning", now, FakeCalendar(), LLM(), load_rules())
    return b["sections"]["supply"]["items"][0]["due"]


async def test_supply_paused_item_not_due(con):
    iid = _item(con, remaining=4)
    _supply.pause(con, iid, _date(2026, 10, 5), _date(2026, 10, 9))
    assert await _due(con) is False


async def test_supply_snoozed_item_not_due(con):
    iid = _item(con, remaining=4)
    _supply.snooze(con, iid, _date(2026, 10, 6), now=NOW)   # snoozed until Wed
    assert await _due(con) is False


async def test_supply_saturday_not_due(con):
    _item(con, remaining=4)
    assert await _due(con, datetime(2026, 10, 10, 8, 30, tzinfo=KST)) is False


async def test_supply_monday_already_reminded_still_due(con):
    iid = _item(con, remaining=4)
    _supply.mark_reminded(con, [iid], now=datetime(2026, 10, 5, 8, 25, tzinfo=KST))
    assert await _due(con, datetime(2026, 10, 5, 8, 30, tzinfo=KST)) is True


def test_supply_brief_due_parity_with_notify_title():
    from pa.core import coach_copy
    assert coach_copy.SUPPLY_BRIEF_DUE == coach_copy.NOTIFY_TITLES["routine_supply"]


# --- _deliver_held rejection ---

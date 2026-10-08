from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pa.core import coach_copy, notify, routine_jobs, supply
from pa.rules import load_rules

KST = timezone(timedelta(hours=9))


def at(y, m, d, h=8, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=KST)


def mk(con, **kw):
    kw.setdefault("label", "샐러드 정기배송")
    kw.setdefault("meals_per_workday", 2)
    kw.setdefault("now", at(2026, 10, 5))
    return supply.add_item(con, **kw)


def _kinds(con):
    return [(n["kind"], n["href"]) for n in notify.unread(con)]


def test_lock_screen_titles_have_no_forbidden_words():
    for k, t in notify._TITLES.items():
        assert coach_copy.notify_safe(t) and coach_copy.notify_safe(notify.ALLOWED[k]), k
        assert "{n}" not in t or k in ("approval_request", "decision_needed")
    assert notify.ALLOWED["routine_supply"] == "/routine"
    for k, t in coach_copy.NOTIFY_TITLES.items():
        assert notify.render(k, 5) == t


def test_is_low_matches_reorder_point(con):
    i = supply.get_item(con, mk(con, remaining=4))
    assert supply.is_low(i, date(2026, 10, 6)) is True
    i = supply.get_item(con, mk(con, remaining=5))
    assert supply.is_low(i, date(2026, 10, 6)) is False


def test_routine_due_morning_daily_and_once(con):
    rules = load_rules()
    assert routine_jobs.routine_due(con, at(2026, 10, 6, 8, 24), rules) == []
    assert routine_jobs.routine_due(con, at(2026, 10, 6, 8, 25), rules) == ["routine_morning"]
    assert routine_jobs.routine_due(con, at(2026, 10, 6, 11, 0), rules) == ["routine_morning"]
    assert routine_jobs.routine_due(con, at(2026, 10, 11, 8, 25), rules) == ["routine_morning"]  # Sunday


async def test_supply_reminder_sent_once_with_fixed_payload(con):
    rules = load_rules()
    mk(con, remaining=6)
    await routine_jobs.run_routine_jobs(con, at(2026, 10, 6, 8, 25), rules)
    assert _kinds(con) == [("routine_supply", "/routine")]
    await routine_jobs.run_routine_jobs(con, at(2026, 10, 6, 8, 26), rules)
    assert len(notify.unread(con)) == 1
    await routine_jobs.run_routine_jobs(con, at(2026, 10, 7, 8, 25), rules)
    assert len(notify.unread(con)) == 1  # same cycle


async def test_quiet_hours_not_sent(con):
    rules = load_rules()
    r = await routine_jobs.notify_routine(con, "routine_supply", 1, "k1", at(2026, 10, 6, 7, 0), rules)
    assert r == "hold" and notify.unread(con) == []
    assert con.execute("SELECT COUNT(*) FROM held_notify WHERE kind='routine_supply'").fetchone()[0] == 1


async def test_sunday_no_reminder_but_consumes(con):
    rules = load_rules()
    iid = mk(con, remaining=3, meals_per_weekend_day=2)
    await routine_jobs.run_routine_jobs(con, at(2026, 10, 11, 8, 25), rules)
    assert notify.unread(con) == []
    assert supply.get_item(con, iid).remaining == 1


async def test_saturday_weekday_gate(con):
    rules = load_rules()
    mk(con, remaining=1, meals_per_weekend_day=1)
    await routine_jobs.run_routine_jobs(con, at(2026, 10, 10, 8, 25), rules)
    assert notify.unread(con) == []  # 'weekday' = Mon-Fri only


def test_notify_never_reads_app_private_data():
    src = (Path(routine_jobs.__file__)).read_text(encoding="utf-8")
    assert "pa.private" not in src and "counselor" not in src


async def test_payload_keys_only(con):
    rules = load_rules()
    mk(con, remaining=2, order_url="https://example.com/order")
    await routine_jobs.run_routine_jobs(con, at(2026, 10, 6, 8, 25), rules)
    assert all(set(n) == {"id", "at", "kind", "title", "href"} for n in notify.unread(con))
    assert "example.com" not in str(notify.unread(con))


async def test_notify_routine_same_dedupe_stored_once(con):
    rules = load_rules()
    t = at(2026, 10, 6, 8, 25)
    await routine_jobs.notify_routine(con, "routine_supply", 1, "dk1", t, rules)
    await routine_jobs.notify_routine(con, "routine_supply", 1, "dk1", t, rules)
    assert len(notify.unread(con)) == 1


async def test_user_scheduled_only_for_routine_kinds(con):
    rules = load_rules()
    with pytest.raises(ValueError):
        await routine_jobs.notify_routine(con, "system", 1, "x", at(2026, 10, 6, 21, 0), rules, user_scheduled=True)
    assert notify.unread(con) == []


async def test_missed_morning_catches_up(con):
    rules = load_rules()
    iid = mk(con, remaining=10)
    assert routine_jobs.routine_due(con, at(2026, 10, 6, 11, 0), rules) == ["routine_morning"]
    await routine_jobs.run_routine_jobs(con, at(2026, 10, 6, 11, 0), rules)
    assert supply.get_item(con, iid).remaining == 8


async def test_step_failure_isolated(con, monkeypatch):
    rules = load_rules()
    mk(con, remaining=2)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(supply, "consume_through", boom)
    await routine_jobs.run_routine_jobs(con, at(2026, 10, 6, 8, 25), rules)
    assert len(notify.unread(con)) == 1  # reminder still evaluated

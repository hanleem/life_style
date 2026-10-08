"""예외 메시지(사용자 데이터가 실릴 수 있음)는 어떤 로그에도 남지 않는다. 클래스 이름과 고정 단계 이름만 남는다."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pa import main
from pa.core import briefing, routine_jobs, worker
from pa.rules import load_rules

MARK = "SECRET-MARKER-제목-7731"
R = load_rules()
NOW = datetime(2026, 10, 6, 8, 0, tzinfo=ZoneInfo("Asia/Seoul"))


class Boom(Exception):
    pass


def boom(*a, **k):
    raise Boom(MARK)


async def aboom(*a, **k):
    raise Boom(MARK)


def _assert_clean(caplog):
    assert caplog.records, "something should be logged"
    for r in caplog.records:
        assert MARK not in r.getMessage()
        assert MARK not in (r.exc_text or "") and r.exc_info is None, r.getMessage()
        assert MARK not in str(r.args)
    assert MARK not in caplog.text
    assert any("Boom" in r.getMessage() for r in caplog.records)


async def test_briefing_and_routine_tick(con, monkeypatch, caplog):
    monkeypatch.setattr(main, "run_due_jobs", aboom)
    monkeypatch.setattr(main, "run_routine_jobs", aboom)
    with caplog.at_level(logging.DEBUG):
        await main.briefing_tick(con, None, R, None, NOW)
        await main.routine_tick(con, R, NOW)
    _assert_clean(caplog)


async def test_scout_tick(con, monkeypatch, caplog):
    from types import SimpleNamespace
    monkeypatch.setattr(main, "effective_config", boom)
    ctx = SimpleNamespace(con=con, rules=R, cfg=None)
    with caplog.at_level(logging.DEBUG):
        await main.scout_tick(ctx, None, NOW)
    _assert_clean(caplog)


async def test_loop_setup_failures(con, monkeypatch, caplog):
    from types import SimpleNamespace
    from pa.scout.config import ScoutConfig
    monkeypatch.setattr(main, "pick_calendars", boom)
    monkeypatch.setattr(main.worker, "run_once", aboom)
    for n in ("routine_tick", "briefing_tick", "scout_tick", "holiday_tick"):
        monkeypatch.setattr(main, n, lambda *a, **k: _noop())
    d = main.LoopDeps(con=con, router=object(), rules=R, auth=SimpleNamespace(), budget=None,
                      settings=SimpleNamespace(), scout_ctx=None, scout_base=ScoutConfig())
    with caplog.at_level(logging.DEBUG):
        try:
            await main.loop_iteration(d, 1)
        except Exception:
            pass
    assert MARK not in caplog.text
    assert caplog.records and all(r.exc_info is None for r in caplog.records)


async def _noop():
    return None


async def test_routine_jobs_exceptions(con, monkeypatch, caplog):
    monkeypatch.setattr(routine_jobs, "routine_due", lambda *a: ["routine_morning"])
    monkeypatch.setattr(routine_jobs, "mark_run", lambda *a: True)
    monkeypatch.setattr(routine_jobs.supply, "consume_through", boom)
    monkeypatch.setattr(routine_jobs.supply, "due_reminders", boom)
    monkeypatch.setattr(routine_jobs, "notify_routine", aboom)
    monkeypatch.setattr(routine_jobs.supply, "MEAL_WEEKDAYS", set(range(7)))
    with caplog.at_level(logging.DEBUG):
        await routine_jobs.run_routine_jobs(con, NOW, R)
    _assert_clean(caplog)


async def test_briefing_job_failure(con, monkeypatch, caplog):
    monkeypatch.setattr(briefing, "build_briefing", aboom)
    monkeypatch.setattr(briefing, "due_jobs", lambda *a, **k: ["brief_morning"])
    monkeypatch.setattr(briefing, "mark_run", lambda *a, **k: True)
    with caplog.at_level(logging.DEBUG):
        await briefing.run_due_jobs(con, NOW, R, None, None)
    _assert_clean(caplog)


async def test_worker_reconcile_never_logs_exception_text(con, caplog):
    from conftest import approve_latest, calendar_draft

    class Cal:
        async def get_by_action(self, _):
            raise Boom(MARK)

    did = calendar_draft(con)
    aid = await approve_latest(con, did)
    con.execute("UPDATE action SET status='unknown' WHERE id=?", (aid,))
    con.commit()
    with caplog.at_level(logging.DEBUG):
        await worker.reconcile_unknown(con, calendar=Cal())
    assert MARK not in caplog.text
    assert any("Boom" in r.getMessage() for r in caplog.records)

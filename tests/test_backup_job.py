import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pa.backup import job, runner
from pa.backup.runner import RunResult
from pa.core.briefing import run_due_jobs
from pa.core import notify
from pa.rules import load_rules
from pa.scheduler import mark_run

KST = ZoneInfo("Asia/Seoul")
R = load_rules(Path(__file__).parent / "fixtures" / "settings_baseline.yaml")
S = SimpleNamespace(backup_key="K")
NOKEY = SimpleNamespace(backup_key="")


def kst(d, h, m=0, day=7):
    return datetime(2026, 10, day, h, m, tzinfo=KST).astimezone(timezone.utc) if d else None


def at(h, m=0, day=7):
    return datetime(2026, 10, day, h, m, tzinfo=KST)


def ok_runner(**kw):
    calls = []

    def f(settings, now):
        calls.append(now)
        return RunResult(run_id="20261006T180000Z", result="ok", files=2, bytes=10, mirror=kw.get("mirror", "off"))
    f.calls = calls
    return f


def fail_runner(reason="io", result="failed"):
    calls = []

    def f(settings, now):
        calls.append(now)
        return RunResult(result=result, reason=reason)
    f.calls = calls
    return f


def _held(con):
    return con.execute("SELECT kind, released_at FROM held_notify").fetchall()


def test_due_at_0300_kst_not_before(con):
    assert not job.backup_due(con, at(2, 59), S)
    assert job.backup_due(con, at(3, 0), S)


def test_catch_up_after_restart(con):
    assert job.backup_due(con, at(11, 20), S)


def test_runs_on_sunday(con):
    assert job.backup_due(con, at(3, 0, day=11), S)


async def test_no_double_run_after_success(con):
    r = ok_runner()
    await job.run_backup_job(con, S, R, at(3, 0), runner=r)
    assert len(r.calls) == 1
    assert not job.backup_due(con, at(9, 0), S)
    assert job.backup_due(con, at(3, 0, day=8), S)


async def test_retry_gap_and_max_attempts(con):
    r = fail_runner()
    got = {}
    for label, t in (("0300", at(3, 0)), ("0330", at(3, 30)), ("0400", at(4, 0)), ("0500", at(5, 0)),
                     ("0600", at(6, 0))):
        got[label] = await job.run_backup_job(con, S, R, t, runner=r) is not None
    assert got == {"0300": True, "0330": False, "0400": True, "0500": True, "0600": False}
    assert len(r.calls) == 3


def test_before_0300_does_not_run_yesterday(con):
    assert not job.backup_due(con, at(1, 0), S)


async def test_no_key_never_due_and_never_notifies(con):
    r = ok_runner()
    assert not job.backup_due(con, at(11, 0), NOKEY)
    assert await job.run_backup_job(con, NOKEY, R, at(11, 0), runner=r) is None
    assert r.calls == [] and _held(con) == [] and notify.unread(con) == []
    assert await job.run_backup_job(con, SimpleNamespace(), R, at(11, 0), runner=r) is None


async def test_runner_executes_in_thread(con):
    seen = {}

    def r(settings, now):
        seen["main"] = threading.current_thread() is threading.main_thread()
        return RunResult(result="ok")
    await job.run_backup_job(con, S, R, at(3, 0), runner=r)
    assert seen == {"main": False}


async def test_single_flight(con, monkeypatch):
    gate = threading.Event()
    calls = []

    def r(settings, now):
        calls.append(1)
        gate.wait(5)
        return RunResult(result="ok")
    monkeypatch.setattr(job, "run_backup", r)
    state = {}
    t1 = job.spawn_backup_tick(state, con, S, R, at(3, 0))
    assert t1 is not None
    await asyncio.sleep(0.1)
    assert job.spawn_backup_tick(state, con, S, R, at(3, 0)) is None
    gate.set()
    await t1
    assert len(calls) == 1


async def test_spawn_not_created_when_not_due(con):
    state = {}
    assert job.spawn_backup_tick(state, con, S, R, at(2, 0)) is None
    assert "task" not in state
    assert job.spawn_backup_tick(state, con, NOKEY, R, at(11, 0)) is None


async def test_spawn_runs_default_runner(con, monkeypatch):
    seen = []
    monkeypatch.setattr(job, "run_backup", lambda s, n: seen.append(n) or RunResult(result="ok"))
    state = {}
    t = job.spawn_backup_tick(state, con, S, R, at(3, 0))
    await t
    assert len(seen) == 1
    assert con.execute("SELECT COUNT(*) FROM job_run WHERE job_id='backup'").fetchone()[0] == 1


async def test_exception_isolated(con):
    def r(settings, now):
        raise RuntimeError("boom /secret/path")
    assert await job.run_backup_job(con, S, R, at(3, 0), runner=r) is None
    ok = ok_runner()
    assert await job.run_backup_job(con, S, R, at(4, 0), runner=ok) is not None


async def test_success_marks_run_but_failure_and_partial_do_not(con):
    await job.run_backup_job(con, S, R, at(3, 0), runner=fail_runner())
    await job.run_backup_job(con, S, R, at(4, 0), runner=fail_runner("low_disk", "partial"))
    assert con.execute("SELECT COUNT(*) FROM job_run WHERE job_id='backup'").fetchone()[0] == 0
    await job.run_backup_job(con, S, R, at(5, 0), runner=ok_runner())
    assert con.execute("SELECT COUNT(*) FROM job_run WHERE job_id='backup'").fetchone()[0] == 1


async def test_failure_notifies_once_with_fixed_payload(con):
    r = fail_runner()
    await job.run_backup_job(con, S, R, at(3, 0), runner=r)
    await job.run_backup_job(con, S, R, at(4, 0), runner=r)
    assert _held(con) == []
    await job.run_backup_job(con, S, R, at(5, 0), runner=r)
    rows = _held(con)
    assert [x["kind"] for x in rows] == ["system"]
    await job.run_backup_job(con, S, R, at(6, 0), runner=r)   # 소진 후엔 더 돌지 않는다
    assert len(_held(con)) == 1
    assert notify.unread(con) == []
    mark_run(con, "brief_morning", at(8, 30))   # 아침 브리핑은 이 테스트의 관심사가 아니다
    await run_due_jobs(con, at(8, 30), R, None, None)
    assert [n["kind"] for n in notify.unread(con)] == ["system"]


async def test_integrity_failure_notifies_immediately(con):
    await job.run_backup_job(con, S, R, at(3, 0), runner=fail_runner("integrity"))
    assert len(_held(con)) == 1


async def test_locked_skip_does_not_notify(con):
    for h in (3, 4, 5):
        await job.run_backup_job(con, S, R, at(h, 0), runner=fail_runner("locked", "skipped"))
    assert _held(con) == []


async def test_mirror_missing_3_days_notifies(con):
    runner.record_result(con, RunResult(result="ok", mirror="missing"), at(3, 0, day=3), attempt=False)
    await job.run_backup_job(con, S, R, at(3, 0), runner=ok_runner(mirror="missing"))
    assert len(_held(con)) == 1


async def test_mirror_missing_1_day_does_not_notify(con):
    runner.record_result(con, RunResult(result="ok", mirror="missing"), at(3, 0, day=6), attempt=False)
    await job.run_backup_job(con, S, R, at(3, 0), runner=ok_runner(mirror="missing"))
    assert _held(con) == []


async def test_stale_success_48h_notifies_only_after_first_ok(con):
    await job.run_backup_job(con, S, R, at(3, 0), runner=fail_runner())
    assert _held(con) == []
    runner.record_result(con, RunResult(result="ok"), at(3, 0, day=3), attempt=False)
    await job.run_backup_job(con, S, R, at(4, 0), runner=fail_runner())
    assert len(_held(con)) == 1


async def test_notify_failure_is_audited(con, monkeypatch):

    async def reject(*a, **k):
        raise RuntimeError("notify down")
    monkeypatch.setattr(job, "notify_gated", reject)
    await job.run_backup_job(con, S, R, at(3, 0), runner=fail_runner("integrity"))
    rows = con.execute("SELECT detail_json FROM audit_log WHERE event='backup.notify_failed'").fetchall()
    assert [r[0] for r in rows] == ["{}"]


async def test_audit_written_with_no_paths(con):
    await job.run_backup_job(con, S, R, at(3, 0), runner=ok_runner())
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE event='backup.run'").fetchone()[0] == 1


def test_job_module_never_reads_app_data():
    base = Path(job.__file__).parent
    for n in ("job.py", "runner.py"):
        text = (base / n).read_text(encoding="utf-8")
        assert "pa.core.todos" not in text and "pa.core.meals" not in text

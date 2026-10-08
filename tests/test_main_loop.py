"""메인 루프: 모든 tick 호출, scout single-flight, scout/briefing 잡 분리."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pa import main
from pa.core.briefing import run_due_jobs
from pa.rules import load_rules
from pa.scheduler import SCOUT_JOBS
from pa.scout.config import OutingCfg, ScoutConfig
from pa.scout import store

KST = ZoneInfo("Asia/Seoul")
R = load_rules()


def _deps(con, scout_ctx=None):
    auth = SimpleNamespace(connected=lambda: False, access_token=lambda: None)
    return main.LoopDeps(con=con, router=object(), rules=R, auth=auth, budget=None,
                         settings=SimpleNamespace(), scout_ctx=scout_ctx, scout_base=ScoutConfig())


async def test_loop_iteration_invokes_every_tick(con, monkeypatch):
    calls = []

    def rec(name):
        async def f(*a, **k):
            calls.append(name)
        return f

    monkeypatch.setattr(main.worker, "run_once", rec("worker"))
    monkeypatch.setattr(main.worker, "reconcile_unknown", rec("reconcile"))
    monkeypatch.setattr(main, "routine_tick", rec("routine"))
    monkeypatch.setattr(main, "briefing_tick", rec("briefing"))
    monkeypatch.setattr(main, "scout_tick", rec("scout"))
    monkeypatch.setattr(main, "holiday_tick", rec("holiday"))
    d = _deps(con)
    await main.loop_iteration(d, 0)
    await d.scout_state["task"]
    assert set(calls) == {"worker", "reconcile", "routine", "briefing", "scout", "holiday"}


async def test_scout_tick_single_flight_and_exception_isolation():
    state, started, release, runs = {}, asyncio.Event(), asyncio.Event(), []
    n = {"i": 0}

    async def slow(ctx, base, now):
        n["i"] += 1
        runs.append(n["i"])
        started.set()
        await release.wait()

    orig = main.scout_tick
    main.scout_tick = slow
    try:
        t1 = main.spawn_scout_tick(state, None, None, None)
        await started.wait()
        assert main.spawn_scout_tick(state, None, None, None) is None   # 실행 중이면 겹치지 않는다
        assert runs == [1]
        release.set()
        await t1
        t2 = main.spawn_scout_tick(state, None, None, None)             # 끝나면 다음 실행 가능
        assert t2 is not None and t2 is not t1
        await t2
        assert runs == [1, 2]
    finally:
        main.scout_tick = orig


async def test_scout_tick_exception_does_not_break_next_spawn(con):
    state = {}
    ctx = SimpleNamespace(con=None, rules=R, cfg=None)   # effective_config 가 예외
    t = main.spawn_scout_tick(state, ctx, ScoutConfig(), datetime.now(KST))
    await t
    assert t.exception() is None
    assert main.spawn_scout_tick(state, ctx, ScoutConfig(), datetime.now(KST)) is not None


async def test_scout_job_runs_once_via_scout_tick_and_briefing_skips_it(con, monkeypatch):
    store.ensure_schema(con)
    now = datetime(2026, 10, 8, 18, 0, tzinfo=KST)   # 목 — scout_outing 시각
    cfg = ScoutConfig(outing=OutingCfg(from_region="서울 종로구 종로3가"))
    ran = await run_due_jobs(con, now, R, None, object())
    assert not (set(ran) & set(SCOUT_JOBS))
    assert all(j.startswith(("brief_", "meal_")) or j == "deliver_held" for j in ran)
    assert con.execute("SELECT COUNT(*) FROM job_run WHERE job_id='scout_outing'").fetchone()[0] == 0

    seen = []

    async def fake_run_job(job, ctx, n):
        seen.append(job)

    monkeypatch.setattr(main, "run_job", fake_run_job)
    ctx = SimpleNamespace(con=con, rules=R, cfg=cfg)
    await main.scout_tick(ctx, cfg, now)
    await main.scout_tick(ctx, cfg, now)
    assert seen.count("scout_outing") == 1
    assert set(seen) <= set(SCOUT_JOBS)




async def test_loop_iteration_spawns_backup_when_due(con, monkeypatch):
    calls = []

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(main.worker, "run_once", noop)
    monkeypatch.setattr(main.worker, "reconcile_unknown", noop)
    monkeypatch.setattr(main, "routine_tick", noop)
    monkeypatch.setattr(main, "briefing_tick", noop)
    monkeypatch.setattr(main, "scout_tick", noop)
    monkeypatch.setattr(main, "spawn_backup_tick", lambda *a, **k: calls.append(a) or None)
    d = _deps(con)
    await main.loop_iteration(d, 1)
    assert len(calls) == 1 and calls[0][0] is d.backup_state


async def test_backup_spawn_failure_does_not_break_loop(con, monkeypatch):
    seen = []

    async def rec(*a, **k):
        seen.append(1)

    monkeypatch.setattr(main.worker, "run_once", rec)
    monkeypatch.setattr(main.worker, "reconcile_unknown", rec)
    monkeypatch.setattr(main, "routine_tick", rec)
    monkeypatch.setattr(main, "briefing_tick", rec)
    monkeypatch.setattr(main, "scout_tick", rec)

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(main, "spawn_backup_tick", boom)
    d = _deps(con)
    await main.loop_iteration(d, 1)
    await d.scout_state["task"]
    assert len(seen) >= 4   # worker, routine, briefing, scout 모두 호출됨







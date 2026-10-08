import pytest

from pa import main


def test_build_refuses_default_secret_in_production(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PA_DEV_SKIP_TAILSCALE", "false")
    monkeypatch.setenv("PA_SESSION_SECRET", "dev-only-change-me")
    with pytest.raises(SystemExit):
        main.build()


def test_build_refuses_short_secret_in_production(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PA_DEV_SKIP_TAILSCALE", "false")
    monkeypatch.setenv("PA_SESSION_SECRET", "short")
    with pytest.raises(SystemExit):
        main.build()


def test_build_ok_with_strong_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PA_DEV_SKIP_TAILSCALE", "false")
    monkeypatch.setenv("PA_SESSION_SECRET", "s" * 40)
    assert main.build() is not None


class _StubRouter:
    async def run(self, *a, **k):
        from pa.llm.router import LLMResult
        return LLMResult({"text": "요약"}, "ai", False)


async def test_briefing_tick_sends_due_morning_briefing(con):
    from datetime import datetime
    from pa.core import notify
    from pa.rules import load_rules
    now = datetime(2026, 10, 6, 8, 31, tzinfo=main.KST)
    await main.briefing_tick(con, _StubRouter(), load_rules(), None, now)
    assert sum(1 for n in notify.unread(con) if n["kind"] == "briefing_morning") == 1


async def test_briefing_tick_swallows_and_logs_errors(con, monkeypatch, caplog):
    from datetime import datetime

    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "run_due_jobs", boom)
    with caplog.at_level("ERROR", logger="pa"):
        await main.briefing_tick(con, None, None, None, datetime.now(main.KST))
    assert any("briefing tick failed" in r.getMessage() for r in caplog.records)


class _StubAuth:
    def __init__(self, connected, granted):
        self._c, self._g = connected, granted

    def connected(self):
        return self._c

    def granted(self):
        return self._g

    async def access_token(self):
        return "T"


def test_calendars_none_when_not_connected():
    assert main.pick_calendars(_StubAuth(False, [])) == (None, None)


def test_calendars_read_only_without_write_scope():
    read, write = main.pick_calendars(_StubAuth(True, ["https://www.googleapis.com/auth/calendar.readonly"]))
    assert read is not None and write is None


def test_calendars_write_with_events_scope():
    read, write = main.pick_calendars(_StubAuth(True, ["https://www.googleapis.com/auth/calendar.events"]))
    assert read is not None and write is read


async def test_routine_tick_swallows_and_logs_errors(con, monkeypatch, caplog):
    from datetime import datetime

    async def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "run_routine_jobs", boom)
    from pa.rules import load_rules
    with caplog.at_level("ERROR", logger="pa"):
        await main.routine_tick(con, load_rules(), datetime(2026, 10, 6, 8, 30, tzinfo=main.KST))
    assert any("routine tick failed" in r.getMessage() for r in caplog.records)


async def test_routine_tick_runs_morning_job(con):
    from datetime import datetime
    from pa.core import notify, supply
    from pa.rules import load_rules
    now = datetime(2026, 10, 6, 8, 30, tzinfo=main.KST)
    supply.add_item(con, label="샐러드 정기배송", meals_per_workday=2, remaining=3, now=now)
    await main.routine_tick(con, load_rules(), now)
    assert [n["kind"] for n in notify.unread(con)] == ["routine_supply"]



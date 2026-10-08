from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from pa.rules import load_rules
from pa.scheduler import due_jobs, mark_run

KST = ZoneInfo("Asia/Seoul")
R = load_rules(Path(__file__).parent / "fixtures" / "settings_baseline.yaml")


def test_morning_due_once_across_restart(con):
    now = datetime(2026, 10, 6, 8, 31, tzinfo=KST)
    assert "brief_morning" in due_jobs(con, now, R)
    assert mark_run(con, "brief_morning", now) is True
    assert "brief_morning" not in due_jobs(con, now, R)      # 재시작 후에도 job_run으로 막힘
    assert mark_run(con, "brief_morning", now) is False


def test_missed_beyond_grace_skipped(con):
    assert "brief_morning" not in due_jobs(con, datetime(2026, 10, 6, 8, 46, tzinfo=KST), R)


def test_friday_only(con):
    assert "brief_friday" in due_jobs(con, datetime(2026, 10, 9, 15, 46, tzinfo=KST), R)
    assert "brief_friday" not in due_jobs(con, datetime(2026, 10, 8, 15, 46, tzinfo=KST), R)


def test_evening_follows_weekday_end(con):
    assert "brief_evening" in due_jobs(con, datetime(2026, 10, 6, 20, 31, tzinfo=KST), R)   # 화 20:30
    assert "brief_evening" not in due_jobs(con, datetime(2026, 10, 7, 20, 31, tzinfo=KST), R)  # 수는 21:30
    assert "brief_evening" in due_jobs(con, datetime(2026, 10, 7, 21, 31, tzinfo=KST), R)


def test_saturday_only_held_bundle(con):
    assert due_jobs(con, datetime(2026, 10, 10, 8, 31, tzinfo=KST), R) == ["deliver_held"]


def test_sunday_nothing(con):
    assert due_jobs(con, datetime(2026, 10, 11, 8, 31, tzinfo=KST), R) == []

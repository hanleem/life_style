from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pa.core.quiet import should_deliver
from pa.rules import load_rules

KST = ZoneInfo("Asia/Seoul")
R = load_rules(Path(__file__).parent / "fixtures" / "settings_baseline.yaml")


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=KST).astimezone(timezone.utc)


def test_tuesday_evening_still_work_until_21():  # 2026-10-06 화요일
    assert should_deliver("decision_needed", at(2026, 10, 6, 19, 30), R) == "deliver"
    assert should_deliver("decision_needed", at(2026, 10, 6, 21, 0), R) == "hold"


def test_wednesday_until_22():                   # 2026-10-07 수요일
    assert should_deliver("decision_needed", at(2026, 10, 7, 21, 30), R) == "deliver"
    assert should_deliver("decision_needed", at(2026, 10, 7, 22, 0), R) == "hold"


def test_saturday_morning_only():                # 2026-10-10 토요일
    assert should_deliver("decision_needed", at(2026, 10, 10, 10), R) == "deliver"
    assert should_deliver("decision_needed", at(2026, 10, 10, 14), R) == "hold"


def test_meditation_block_held():               # 08:00-08:15 보호 시간
    assert should_deliver("decision_needed", at(2026, 10, 6, 8, 5), R) == "hold"
    assert should_deliver("decision_needed", at(2026, 10, 6, 8, 16), R) == "deliver"


def test_public_holiday_is_workday():           # 2026-10-09 한글날(금) - 공휴일 목록 미사용
    assert should_deliver("decision_needed", at(2026, 10, 9, 10), R) == "deliver"


def test_urgent_passes_at_night():
    assert should_deliver("lab_urgent", at(2026, 10, 6, 23), R, urgent=True) == "deliver"


def test_sunday_rest_day():
    assert should_deliver("decision_needed", at(2026, 10, 11, 10), R) == "hold"


def test_workday_decision_delivers_and_info_folds():
    assert should_deliver("decision_needed", at(2026, 10, 6, 10), R) == "deliver"
    assert should_deliver("info", at(2026, 10, 6, 10), R) == "fold"


def test_user_requested_reply_passes_evening():
    assert should_deliver("approval_request", at(2026, 10, 6, 20), R, user_requested=True) == "deliver"


def test_rules_derived_values():
    from datetime import time
    assert R.work_end_on(1) == time(21, 0) and R.work_end_on(2) == time(22, 0)
    assert R.work_end_on(5) == time(13, 0) and R.work_end_on(6) is None
    assert R.brief_evening_on(1) == time(20, 30) and R.brief_evening_on(2) == time(21, 30)
    assert R.brief_evening_on(5) is None and R.brief_evening_on(6) is None

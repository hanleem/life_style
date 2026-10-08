"""가족의 기본 일정(config/settings.yaml)이 의도한 값인지 고정한다. 다른 테스트는 기준 일정을 쓴다."""
from datetime import time

import pytest

from pa.rules import load_rules
from pa.scheduler import _sched_times

pytestmark = pytest.mark.real_config


def test_real_morning_flow():
    r = load_rules()
    assert r.work_start == time(7, 0)          # 07:00 하루 시작
    assert r.protected_until == time(7, 30)    # 07:30 브리핑까지 알림 보류
    assert r.brief_morning == time(7, 30)


def test_real_weekly_jobs_are_scheduled():
    thu = _sched_times(load_rules(), 3)
    assert thu["brief_morning"] == time(7, 30) and thu["meal_plan"] == time(9, 30)   # 목요일 식단 생성
    assert _sched_times(load_rules(), 4)["brief_friday"] == time(17, 0)               # 금요일 주간 정리

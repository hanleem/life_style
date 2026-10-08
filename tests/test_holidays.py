"""휴일 캘린더(대한민국의 휴일)를 식사 공급 계산에 반영한다."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pa.core import holidays, supply
from pa.integrations.calendar import CalEvent, FakeCalendar

KST = ZoneInfo("Asia/Seoul")
FRI = date(2026, 10, 9)           # 한글날(금)
NOW = datetime(2026, 10, 7, 9, 0, tzinfo=KST)


def _item(con, remaining=3):
    return supply.add_item(con, label="샐러드", meals_per_workday=1, meals_per_weekend_day=0,
                           lead_days=3, safety_days=1, remaining=remaining, now=NOW)


def _holiday_event(day: date, title="한글날", kind="공휴일"):
    s = datetime(day.year, day.month, day.day, tzinfo=KST)
    return CalEvent(f"h{day}", "ko", title, s, s + timedelta(days=1), True, kind)


def test_holiday_counts_like_weekend(con):
    it = supply.get_item(con, _item(con))
    assert supply.meals_on(it, FRI) == 1
    supply.set_holidays({FRI})
    assert supply.meals_on(it, FRI) == 0
    assert supply.meals_on(it, date(2026, 10, 8)) == 1          # 목요일은 그대로
    assert supply.is_meal_day(FRI) is False


def test_holiday_not_consumed_and_not_due(con):
    iid = _item(con, remaining=3)
    supply.set_holidays({FRI})
    supply.consume_through(con, date(2026, 10, 9), now=NOW + timedelta(days=2))
    assert supply.get_item(con, iid).remaining == 3             # 오늘(금) 휴일: 차감 없음
    assert supply.is_due_today(supply.get_item(con, iid), FRI) is False


async def test_refresh_stores_and_applies(con):
    cal = FakeCalendar()
    cal.events = [_holiday_event(FRI)]
    n = await holidays.refresh(con, cal, date(2026, 10, 7), NOW)
    assert n == 1 and supply.is_meal_day(FRI) is False
    supply.set_holidays(())                                      # 재시작 흉내: 저장본을 다시 올린다
    assert holidays.load(con) == 1 and supply.is_meal_day(FRI) is False


async def test_refresh_failure_keeps_previous(con):
    cal = FakeCalendar()
    cal.events = [_holiday_event(FRI)]
    await holidays.refresh(con, cal, date(2026, 10, 7), NOW)

    class Boom:
        async def events_between(self, *a):
            raise ConnectionError("Google 미연결")

    assert await holidays.refresh(con, Boom(), date(2026, 10, 7), NOW) == -1
    assert supply.is_meal_day(FRI) is False                      # 읽기 실패 시 이전 값 유지


async def test_observance_is_not_a_day_off(con):
    """국군의날 같은 '기념일'은 쉬는 날이 아니다. 설명 첫 줄이 '공휴일'인 것만 쓴다."""
    cal = FakeCalendar()
    cal.events = [_holiday_event(date(2026, 10, 1), "국군의날", kind="기념일"), _holiday_event(FRI)]
    assert await holidays.refresh(con, cal, date(2026, 10, 7), NOW) == 1
    assert supply.is_meal_day(date(2026, 10, 1)) is True and supply.is_meal_day(FRI) is False

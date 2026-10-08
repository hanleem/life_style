from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from pa.core.dateparse import find_time, resolve_date

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))     # 목요일


def test_resolve_date_relative_and_weekday_words():
    assert resolve_date("오늘 회의", NOW) == date(2026, 10, 8)
    assert resolve_date("내일 3시", NOW) == date(2026, 10, 9)
    assert resolve_date("모레", NOW) == date(2026, 10, 10)
    assert resolve_date("금요일 오후 2시", NOW) == date(2026, 10, 9)
    assert resolve_date("목요일에 보자", NOW) == date(2026, 10, 15)          # 오늘이 목요일이면 다음 주 목요일
    assert resolve_date("다음 주 월요일까지", NOW) == date(2026, 10, 12)
    assert resolve_date("다음주 월요일까지", NOW) == date(2026, 10, 12)
    assert resolve_date("이번 주 금요일", NOW) == date(2026, 10, 9)
    assert resolve_date("다다음 주 수요일", NOW) == date(2026, 10, 21)
    assert resolve_date("그냥 인사", NOW) is None


def test_find_time_variants():
    assert find_time("목요일 오후 4시") == time(16, 0)
    assert find_time("오후 4시 반") == time(16, 30)
    assert find_time("16:30에") == time(16, 30)
    assert find_time("오전 9시 15분") == time(9, 15)
    assert find_time("금요일 4시") == time(16, 0)          # 접두어 없는 1~6시는 오후
    assert find_time("10시에") == time(10, 0)
    assert find_time("시간 미정") is None


def test_last_week_words_resolve_to_the_past():
    assert resolve_date("지난 목요일 회의", NOW) == date(2026, 10, 1)
    assert resolve_date("저번 주 월요일", NOW) == date(2026, 10, 5)


def test_dates_in_finds_explicit_and_relative_dates():
    from pa.core.dateparse import dates_in
    assert date(2026, 10, 15) in dates_in("회의는 10월 15일 오후 3시입니다", NOW)
    assert date(2026, 10, 15) in dates_in("회의 2026-10-15 15:00", NOW)
    assert date(2026, 10, 15) in dates_in("Meeting on Oct 15 at 3pm", NOW)
    assert date(2026, 10, 15) in dates_in("see you 15 October", NOW)
    assert date(2026, 10, 15) in dates_in("일정: 10/15 3시", NOW)
    assert date(2026, 10, 9) in dates_in("내일 뵙겠습니다", NOW)
    assert date(2027, 1, 5) in dates_in("1월 5일에 마감", NOW)                       # 두 달 넘게 지난 날짜는 내년으로
    assert dates_in("그냥 안부 메일입니다", NOW) == set()
    assert dates_in("13월 40일", NOW) == set()                                       # 말이 안 되는 날짜는 무시


def test_explicit_date_beats_a_weekday_word():
    """'10월 23일 금요일' 은 가장 가까운 금요일(10/9)이 아니라 10월 23일이다(실제로 틀렸던 사례)."""
    from pa.core.dateparse import pick_date
    assert pick_date("학교 행사 날짜가 10월 23일 금요일로 변경됨.", NOW) == date(2026, 10, 23)
    assert pick_date("10월 23일(금) 학교 행사", NOW) == date(2026, 10, 23)
    assert pick_date("Lab tour moved to Friday, Oct 23", NOW) == date(2026, 10, 23)
    assert pick_date("2026-10-23 학교 행사", NOW) == date(2026, 10, 23)


def test_relative_words_are_used_only_without_an_explicit_date():
    from pa.core.dateparse import pick_date
    assert pick_date("금요일에 학교 행사", NOW) == date(2026, 10, 9)
    assert pick_date("다음 주 월요일까지 제출", NOW) == date(2026, 10, 12)
    assert pick_date("내일 3시", NOW) == date(2026, 10, 9)
    assert pick_date("안부 인사", NOW) is None


def test_several_explicit_dates_trust_the_models_pick_only_if_it_is_one_of_them():
    from pa.core.dateparse import pick_date
    text = "원래 10월 16일이었는데 10월 23일로 변경됨"
    assert pick_date(text, NOW, hint=date(2026, 10, 23)) == date(2026, 10, 23)
    assert pick_date(text, NOW, hint=date(2026, 10, 9)) == date(2026, 10, 16)      # 힌트가 본문에 없으면 가장 이른 날짜
    assert pick_date("마감 10월 13일", NOW, hint=date(2026, 10, 9)) == date(2026, 10, 13)   # 하나뿐이면 힌트보다 본문

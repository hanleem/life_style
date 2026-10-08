"""due_parse — 결정적 한국어 날짜·시각 해석 (Task B1p). 순수 함수, 가짜 시계(at 주입)."""
import dataclasses
import random
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pa.core import due_parse
from pa.core.due_parse import Due, due_confidence, resolve_due

KST = ZoneInfo("Asia/Seoul")
MSG = datetime(2026, 10, 6, 14, 0, tzinfo=KST)  # 화요일
WD = "월화수목금토일"


def kst(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=KST)


def r(expr, at=MSG, **kw):
    return resolve_due(expr, at, **kw)


def ok(expr, want, all_day, at=MSG):
    due = r(expr, at)
    assert due is not None, expr
    assert due.at == want, (expr, due.at.astimezone(KST), want)
    assert due.all_day is all_day, expr
    assert due.at.utcoffset() == timedelta(0)  # UTC 저장
    return due


# --------------------------------------------------------------------------- 계획의 기본 케이스

def test_tomorrow_3pm():
    due = ok("내일 3시", kst(2026, 10, 7, 15), False)
    assert due.label == "10/07(수) 15:00"
    assert isinstance(due, Due)


def test_bare_weekday_next_occurrence():
    due = ok("금요일", kst(2026, 10, 9), True)
    assert due.label == "10/09(금)"


def test_bare_weekday_same_day_is_none():
    assert r("화요일") is None


def test_this_week_past_day_is_none():
    assert r("이번 주 월요일") is None


def test_next_week_weekday():
    ok("다음 주 수요일", kst(2026, 10, 14), True)


def test_month_day_and_slash():
    for e in ("10월 8일", "10/8", "10.8", "10월8일", "10/08"):
        ok(e, kst(2026, 10, 8), True)


def test_past_date_rolls_to_next_year_within_60_days():
    ok("1/3", kst(2027, 1, 3), True, at=kst(2026, 12, 30, 9))
    assert r("3/1") is None


def test_small_hour_assumed_pm():
    ok("3시", kst(2026, 10, 6, 15), False)
    ok("내일 오전 3시", kst(2026, 10, 7, 3), False)
    ok("내일 10시", kst(2026, 10, 7, 10), False)


def test_half_hour():
    ok("내일 2시 반", kst(2026, 10, 7, 14, 30), False)


def test_time_only_before_message_time_is_none():
    assert r("11시에") is None
    ok("4시에", kst(2026, 10, 6, 16), False)


def test_vague_words_none():
    for e in ("나중에", "이번 주 안에", "조만간"):
        assert r(e) is None


def test_kst_midnight_boundary():
    at = datetime(2026, 10, 6, 23, 50, tzinfo=KST)
    assert at.astimezone(UTC).hour == 14
    ok("내일", kst(2026, 10, 7), True, at=at)
    # 같은 순간을 UTC 로 줘도 같다
    ok("내일", kst(2026, 10, 7), True, at=at.astimezone(UTC))


def test_none_and_empty_input():
    assert r(None) is None
    assert r("") is None
    assert r("   ") is None
    assert r("\u200b") is None
    assert r(123) is None  # type: ignore[arg-type]


def test_naive_base_rejected():
    with pytest.raises(ValueError):
        resolve_due("내일", datetime(2026, 10, 6, 14, 0))


# --------------------------------------------------------------------------- 날짜 단어

@pytest.mark.parametrize("expr,delta", [
    ("오늘", 0), ("금일", 0), ("내일", 1), ("낼", 1), ("명일", 1), ("모레", 2), ("글피", 3),
    ("내일모레", 2), ("오늘까지", 0), ("내일까지", 1), ("모레까지", 2), ("내일은", 1),
    ("3일 후", 3), ("3일 뒤", 3), ("1주 후", 7), ("2주 뒤", 14),
])
def test_relative_day_words(expr, delta):
    d = date(2026, 10, 6) + timedelta(days=delta)
    ok(expr, kst(d.year, d.month, d.day), True)


def test_today_with_future_time_ok_and_past_time_none():
    ok("오늘 오후 5시", kst(2026, 10, 6, 17), False)
    assert r("오늘 오전 9시") is None
    assert r("오늘 오후 2시") is None  # 메시지 시각과 같으면 이미 지난 것


def test_낼_inside_word_is_not_a_date():
    assert r("끝낼") is None
    ok("끝낼 때 3시", kst(2026, 10, 6, 15), False)  # 낼 이 내일로 읽히지 않는다(시각만 → 오늘)


def test_this_week_and_next_week_weekdays():
    ok("이번 주 금요일", kst(2026, 10, 9), True)
    ok("이번주 금요일", kst(2026, 10, 9), True)
    ok("금주 일요일", kst(2026, 10, 11), True)
    ok("이번 주 화요일", kst(2026, 10, 6), True)  # 오늘
    ok("다음 주 월요일", kst(2026, 10, 12), True)
    ok("다음주 월요일", kst(2026, 10, 12), True)
    ok("담주 금요일", kst(2026, 10, 16), True)
    ok("차주 월요일", kst(2026, 10, 12), True)
    ok("다다음 주 월요일", kst(2026, 10, 19), True)
    ok("다음 주 수요일 오후 3시", kst(2026, 10, 14, 15), False)
    ok("이번 주 금요일까지", kst(2026, 10, 9), True)


@pytest.mark.parametrize("wd,expect_day", [
    ("수요일", 7), ("목요일", 8), ("금요일", 9), ("토요일", 10), ("일요일", 11), ("월요일", 12),
])
def test_bare_weekday_all(wd, expect_day):
    ok(wd, kst(2026, 10, expect_day), True)


def test_week_starts_on_monday_around_sunday():
    sun = kst(2026, 10, 11, 10)
    ok("이번 주 일요일", kst(2026, 10, 11), True, at=sun)
    assert r("이번 주 월요일", sun) is None  # 같은 주(월 시작)의 과거
    ok("다음 주 월요일", kst(2026, 10, 12), True, at=sun)
    mon = kst(2026, 10, 5, 9)
    ok("이번 주 월요일", kst(2026, 10, 5), True, at=mon)
    assert r("월요일", mon) is None  # 같은 요일은 애매
    ok("금요일", kst(2026, 10, 9), True, at=mon)


def test_weekday_with_time():
    ok("금요일 오후 2시", kst(2026, 10, 9, 14), False)
    ok("금요일 14:30", kst(2026, 10, 9, 14, 30), False)


def test_weekday_consistent_with_explicit_date_else_conflict():
    ok("10월 8일 목요일", kst(2026, 10, 8), True)
    ok("10/9(금)", kst(2026, 10, 9), True)
    ok("10/9 (금) 오후 3시", kst(2026, 10, 9, 15), False)
    assert r("10월 8일 금요일") is None
    assert r("10/9(목)") is None
    assert r("내일 금요일") is None
    ok("내일 수요일", kst(2026, 10, 7), True)
    ok("이번 주 금요일 10/9", kst(2026, 10, 9), True)
    assert r("이번 주 금요일 10/8") is None


def test_month_day_formats():
    ok("10월 20일", kst(2026, 10, 20), True)
    ok("11월 3일까지", kst(2026, 11, 3), True)
    ok("10/20", kst(2026, 10, 20), True)
    ok("10.20", kst(2026, 10, 20), True)
    ok("1.3", kst(2027, 1, 3), True, at=kst(2026, 12, 30, 9))
    ok("2026-10-08", kst(2026, 10, 8), True)
    ok("2026/10/8", kst(2026, 10, 8), True)
    ok("2026.10.08", kst(2026, 10, 8), True)
    ok("2026년 10월 8일", kst(2026, 10, 8), True)
    ok("2027-03-01", kst(2027, 3, 1), True)


def test_slash_and_dot_march_14():
    at = kst(2026, 1, 5, 9)
    ok("3/14", kst(2026, 3, 14), True, at=at)
    ok("3.14", kst(2026, 3, 14), True, at=at)
    ok("3월 14일", kst(2026, 3, 14), True, at=at)
    ok("3/14 오후 3시", kst(2026, 3, 14, 15), False, at=at)


def test_today_explicit_date_is_today():
    ok("10/6", kst(2026, 10, 6), True)
    ok("10월 6일", kst(2026, 10, 6), True)
    ok("2026-10-06", kst(2026, 10, 6), True)


def test_past_explicit_dates_none():
    assert r("10/5") is None
    assert r("2026-10-05") is None
    assert r("2025-12-25") is None
    assert r("1999-12-25") is None
    assert r("2101-01-01") is None


def test_day_of_month_only():
    ok("20일", kst(2026, 10, 20), True)
    ok("6일", kst(2026, 10, 6), True)       # 오늘
    ok("3일", kst(2026, 11, 3), True)        # 이번 달은 지났으니 다음 달
    ok("31일", kst(2026, 10, 31), True)
    assert r("31일", kst(2026, 11, 6, 9)) is None   # 11월엔 31일이 없다 — 추측 금지
    assert r("0일") is None
    assert r("32일") is None
    ok("5일", kst(2027, 1, 5), True, at=kst(2026, 12, 20, 9))
    assert r("30일", kst(2026, 1, 31, 9)) is None  # 1/31 → 2월 30일은 없다


@pytest.mark.parametrize("expr", ["3일간", "3일 동안", "3일 전", "3일째", "3일 이내", "2주 전", "3시간 후", "30분 후"])
def test_durations_and_ago_are_not_dates(expr):
    assert r(expr) is None


@pytest.mark.parametrize("expr", ["13/45", "0/5", "2/30", "13월 1일", "2월 30일", "4/31", "2026-13-01", "2026-02-30"])
def test_invalid_dates_none(expr):
    assert r(expr) is None


# --------------------------------------------------------------------------- 롤오버·윤일

def test_midnight_month_year_rollover():
    ok("내일", kst(2026, 11, 1), True, at=kst(2026, 10, 31, 23, 59))
    ok("모레", kst(2026, 11, 1), True, at=kst(2026, 10, 30, 12))
    ok("내일", kst(2027, 1, 1), True, at=kst(2026, 12, 31, 22))
    ok("모레", kst(2027, 1, 2), True, at=kst(2026, 12, 31, 22))
    ok("글피", kst(2027, 1, 3), True, at=kst(2026, 12, 31, 22))
    ok("내일", kst(2026, 3, 1), True, at=kst(2026, 2, 28, 10))  # 2026 은 평년
    ok("금요일", kst(2027, 1, 1), True, at=kst(2026, 12, 29, 10))  # 화 → 금(1/1)
    ok("3일 후", kst(2027, 1, 2), True, at=kst(2026, 12, 30, 10))


def test_leap_day():
    ok("내일", kst(2028, 2, 29), True, at=kst(2028, 2, 28, 10))
    ok("모레", kst(2028, 3, 1), True, at=kst(2028, 2, 28, 10))
    ok("내일", kst(2028, 3, 1), True, at=kst(2028, 2, 29, 10))
    ok("2/29", kst(2028, 2, 29), True, at=kst(2028, 1, 10, 9))
    ok("2월 29일", kst(2028, 2, 29), True, at=kst(2028, 1, 10, 9))
    assert r("2/29", kst(2027, 1, 10, 9)) is None            # 평년 — 존재하지 않는 날
    ok("2/29", kst(2028, 2, 29), True, at=kst(2027, 12, 31, 10))  # 60일 안
    assert r("2/29", kst(2027, 12, 30, 10)) is None              # 61일
    assert r("2027-02-29") is None


def test_rollover_window_edges():
    # 12/31 에서 2/28 은 59일 → 다음 해, 3/2 는 61일 → None
    ok("2/28", kst(2027, 2, 28), True, at=kst(2026, 12, 31, 10))
    ok("3/1", kst(2027, 3, 1), True, at=kst(2026, 12, 31, 10))   # 60일
    assert r("3/2", kst(2026, 12, 31, 10)) is None               # 61일 → 이미 지난 날짜로 보지 않음
    ok("1/1", kst(2027, 1, 1), True, at=kst(2026, 12, 31, 10))


# --------------------------------------------------------------------------- 시각

@pytest.mark.parametrize("expr,h,mi", [
    ("오전 9시", 9, 0), ("오전 11시 30분", 11, 30), ("오후 1시", 13, 0), ("오후 3시 반", 15, 30),
    ("오후 12시", 12, 0), ("오후 11시", 23, 0), ("낮 12시", 12, 0), ("낮 2시", 14, 0),
    ("저녁 6시", 18, 0), ("저녁 7시 30분", 19, 30), ("밤 9시", 21, 0), ("밤 11시 반", 23, 30),
    ("새벽 2시", 2, 0), ("새벽 5시 반", 5, 30), ("아침 7시", 7, 0), ("아침 9시 15분", 9, 15),
    ("15:30", 15, 30), ("09:05", 9, 5), ("23:59", 23, 59), ("오후 3:30", 15, 30),
    ("오후 3시 5분", 15, 5), ("15시", 15, 0), ("15시 30분", 15, 30), ("정오", 12, 0),
    ("12시", 12, 0), ("7시", 7, 0), ("11시 59분", 11, 59), ("1시", 13, 0), ("6시 반", 18, 30),
    ("03:30", 3, 30),  # 0 패딩 24시간제는 그대로
    ("3:30", 15, 30),  # D7
    ("오전 0시", 0, 0), ("새벽 0시", 0, 0),
])
def test_time_phrases_tomorrow(expr, h, mi):
    ok(f"내일 {expr}", kst(2026, 10, 7, h, mi), False)


def test_d7_boundaries():
    # 1~6 → 오후, 7~11 → 오전, 12 → 정오
    for h in range(1, 7):
        ok(f"내일 {h}시", kst(2026, 10, 7, h + 12), False)
    for h in range(7, 12):
        ok(f"내일 {h}시", kst(2026, 10, 7, h), False)
    ok("내일 12시", kst(2026, 10, 7, 12), False)
    # 오전/오후가 있으면 D7 적용 안 함
    ok("내일 오전 1시", kst(2026, 10, 7, 1), False)
    ok("내일 오후 7시", kst(2026, 10, 7, 19), False)


def test_midnight_and_noon():
    due = ok("내일 자정", kst(2026, 10, 8, 0), False)   # 자정 = 내일의 24:00
    assert due.label == "10/08(목) 00:00"
    ok("내일 밤 12시", kst(2026, 10, 8, 0), False)
    ok("내일 24시", kst(2026, 10, 8, 0), False)
    ok("내일 0시", kst(2026, 10, 7, 0), False)
    ok("내일 정오", kst(2026, 10, 7, 12), False)
    ok("정오", kst(2026, 10, 6, 12), False, at=kst(2026, 10, 6, 9))
    assert r("정오") is None  # 14:00 메시지에서 정오는 이미 지남
    ok("자정까지", kst(2026, 10, 7, 0), False)  # 오늘 밤 24:00


@pytest.mark.parametrize("expr", [
    "내일 오전 12시",    # 애매 (정오? 자정?)
    "내일 새벽 12시",
    "내일 오후 0시", "내일 오후 13시", "내일 오전 13시",
    "내일 25시", "내일 24시 30분", "내일 3시 60분", "내일 99:99", "내일 25:00", "내일 15:60",
    "내일 낮 7시", "내일 저녁 2시", "내일 밤 3시", "내일 아침 3시", "내일 새벽 8시",
    "내일 오후", "내일 아침", "내일 저녁", "내일 밤", "내일 새벽", "내일 오전",
])
def test_impossible_or_ambiguous_times_none(expr):
    assert r(expr) is None


def test_korean_numeral_hours_are_not_guessed():
    for e in ("내일 세시", "내일 오후 두 시", "내일 열두시"):
        assert r(e) is None


def test_banan_is_not_half():
    assert r("내일 3시 반드시") is not None  # '반드시' 는 반(30분)이 아니다
    ok("내일 3시 반드시", kst(2026, 10, 7, 15), False)
    ok("내일 3시 반에", kst(2026, 10, 7, 15, 30), False)
    ok("내일 3시반", kst(2026, 10, 7, 15, 30), False)


def test_time_only_today_and_tomorrow_semantics():
    ok("오후 5시", kst(2026, 10, 6, 17), False)
    ok("17:00", kst(2026, 10, 6, 17), False)
    assert r("오전 10시") is None
    assert r("14:00") is None
    ok("14:01", kst(2026, 10, 6, 14, 1), False)


def test_time_range_uses_start():
    ok("내일 2시~4시", kst(2026, 10, 7, 14), False)
    ok("내일 2시 ~ 4시", kst(2026, 10, 7, 14), False)
    ok("내일 오후 2시~4시", kst(2026, 10, 7, 14), False)
    ok("내일 오전 10시~12시", kst(2026, 10, 7, 10), False)
    ok("내일 오전 10시~2시", kst(2026, 10, 7, 10), False)   # 끝은 D7 로 14시
    ok("내일 오후 2시~10시", kst(2026, 10, 7, 14), False)   # 끝은 오후 상속
    ok("내일 2시부터 4시까지", kst(2026, 10, 7, 14), False)
    ok("내일 14:00-16:00", kst(2026, 10, 7, 14), False)
    ok("내일 14:00~16:00", kst(2026, 10, 7, 14), False)
    ok("내일 9시에서 11시", kst(2026, 10, 7, 9), False)
    assert r("내일 4시~2시") is None          # 끝이 시작보다 이름
    assert r("내일 오후 3시~오전 5시") is None
    assert r("내일 3시~3시") is None
    assert r("내일 2시~4시~6시") is None


def test_conflicting_times_none():
    assert r("내일 3시 4시") is None
    assert r("내일 오전 10시 오후 3시") is None
    ok("내일 3시 오후 3시", kst(2026, 10, 7, 15), False)   # 같은 시각 반복은 충돌 아님
    ok("내일 15:00 오후 3시", kst(2026, 10, 7, 15), False)


def test_conflicting_dates_none():
    assert r("내일 모레") is None
    assert r("10/8 10/9") is None
    assert r("10/8~10/9") is None
    assert r("오늘 내일") is None
    assert r("10월 8일 내일") is None
    assert r("이번 주 금요일 다음 주 월요일") is None
    assert r("금요일 토요일") is None
    assert r("내일 10월 9일") is None
    ok("10월 7일 내일", kst(2026, 10, 7), True)  # 같은 날짜를 두 번 말한 것 — 충돌 아님


def test_approximate_times_none():
    for e in ("내일 3시쯤", "내일 3시경", "내일 오후 3시 무렵", "10/8경", "10월 8일쯤"):
        assert r(e) is None


# --------------------------------------------------------------------------- 과거형·막연·반복

@pytest.mark.parametrize("expr", [
    "어제", "어제 3시", "그제", "그저께", "엊그제", "지난 금요일", "지난주 월요일", "작년", "아까",
    "방금", "어제까지", "어제 오후 2시", "어제오늘", "3일 전", "전날",
])
def test_past_phrases_none(expr):
    assert r(expr) is None


@pytest.mark.parametrize("expr", [
    "다음에", "담에", "조만간", "나중에", "이따", "이따가 3시", "곧", "언제", "언제든", "주말", "이번 주말",
    "주중", "평일", "시간 될 때", "시간 날 때", "시간 되면", "여유 있을 때", "되는 대로", "가능할 때",
    "빨리", "최대한 빨리", "ASAP", "asap", "이번 주 안에", "이번 주까지", "이번주 안으로", "금주 중", "다음 주",
    "다음 주 3시", "이번 달", "다음 달 5일 이전", "다음달", "월말", "월초", "연말", "내년", "올해 안에",
    "추후", "차차", "언젠가", "일주일 후", "이틀 뒤", "하루 이틀", "며칠 내", "연휴", "휴일",
    "오전 중", "오후 중으로", "내일 오전 중", "10월", "3월까지", "10월 중", "2026년",
])
def test_vague_phrases_none(expr):
    assert r(expr) is None


def test_vague_plus_explicit_is_still_none():
    # 막연한 표현이 하나라도 섞이면 추측하지 않는다
    assert r("내일 아니면 나중에") is None
    assert r("내일 3시 이따가") is None
    assert r("어제 아니고 내일") is None


@pytest.mark.parametrize("expr", ["매주 금요일", "매일 3시", "매월 5일", "격주 월요일", "금요일마다", "정기 보고"])
def test_recurring_is_not_a_due(expr):
    assert r(expr) is None


def test_garbage_and_non_date_text_none():
    for e in ("ㅋㅋㅋ", "hello", "확인 부탁드립니다", "회의록", "v3.14", "버전 1.2.3", "010-1234-5678", "3.5배", "10%", "1234"):
        assert r(e) is None, e


def test_unparsed_digits_never_guessed():
    assert r("내일 3 시") is not None            # 공백 허용
    assert r("내일 12345") is None
    assert r("내일 3번") is None
    assert r("내일 3명") is None


# --------------------------------------------------------------------------- 원문 포함 검사

def test_phrase_must_appear_in_message():
    msg = "내일 오후 3시에 회의 가능할까요?"
    assert r("내일 오후 3시", message=msg) is not None
    assert r("내일 오후 3시", message="모레 보자") is None          # 환각 날짜 차단
    assert r("금요일", message=msg) is None
    assert r("내일", message=msg) is not None
    assert r("내일 4시", message=msg) is None
    assert r("내일 오후 3시", message="") is None
    assert r("내일 오후 3시", message="오후 3시 내일") is None       # 순서가 다르면 다른 문장


def test_phrase_in_message_ignores_spacing_and_unicode_forms():
    assert r("내일 오후 3시", message="내일  오후3시에 봬요") is not None
    assert r("내일오후3시", message="내일 오후 3시") is not None
    assert r("내일 3시", message="내\u200b일 ３시 에 봐") is not None     # 제로폭·전각
    assert r("내일 ３시", message="내일 3시") is not None
    assert r("내일 3시", message="﻿내일 3시") is not None
    assert r("ASAP 내일", message="asap 내일") is None  # 막연 표현 우선 (None 이어야 함)


def test_message_none_skips_containment():
    assert r("내일 3시", message=None) is not None


def test_message_given_but_empty_phrase_rejected():
    assert r("", message="내일") is None
    assert r("\u200b", message="내일") is None


def test_non_string_message_rejected():
    assert r("내일", message=123) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- 유니코드

def test_nfkc_fullwidth_and_zero_width():
    ok("내일 １５：３０", kst(2026, 10, 7, 15, 30), False)
    ok("１０/８", kst(2026, 10, 8), True)
    ok("１０월 ８일", kst(2026, 10, 8), True)
    ok("내\u200b일 3‌시", kst(2026, 10, 7, 15), False)
    ok("﻿내일⁠ 3시", kst(2026, 10, 7, 15), False)
    ok("내일　오후 3시", kst(2026, 10, 7, 15), False)       # 전각·NBSP 공백
    ok("내일 오후 ３시 ３０분", kst(2026, 10, 7, 15, 30), False)
    ok("２０２６－１０－０８", kst(2026, 10, 8), True)
    ok("내일\t오후\n3시", kst(2026, 10, 7, 15), False)


def test_nfd_hangul_composes():
    import unicodedata
    nfd = unicodedata.normalize("NFD", "내일 오후 3시")
    assert nfd != "내일 오후 3시"
    ok(nfd, kst(2026, 10, 7, 15), False)
    assert r(nfd, message=unicodedata.normalize("NFC", "내일 오후 3시에")) is not None


def test_bidi_and_control_chars_ignored():
    ok("‮내일‬ 3시", kst(2026, 10, 7, 15), False)
    ok("내일\x00 3시", kst(2026, 10, 7, 15), False)
    ok("내일\x07 3시", kst(2026, 10, 7, 15), False)


# --------------------------------------------------------------------------- 출력 모양

def test_output_shape():
    due = ok("내일 3시", kst(2026, 10, 7, 15), False)
    assert due.at.tzinfo is not None and due.at.utcoffset() == timedelta(0)
    assert due.due_confidence == "explicit"
    assert due_confidence(due) == "explicit"
    assert due_confidence(None) == "unknown"
    allday = r("내일")
    assert allday.all_day is True
    assert allday.at == kst(2026, 10, 7)                       # 그날 KST 00:00 (UTC 로는 전날 15:00)
    assert allday.at.astimezone(UTC) == datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
    assert allday.label == "10/07(수)"
    with pytest.raises(dataclasses.FrozenInstanceError):
        allday.all_day = False  # frozen


def test_label_weekday_names():
    for i, wd in enumerate(WD):
        d = date(2026, 10, 5) + timedelta(days=i)
        assert r(f"{d.month}/{d.day}", kst(2026, 10, 5, 8)).label == f"{d:%m/%d}({wd})"


def test_same_instant_any_timezone_same_result():
    a = r("내일 3시", MSG)
    b = r("내일 3시", MSG.astimezone(UTC))
    c = r("내일 3시", MSG.astimezone(ZoneInfo("America/Los_Angeles")))
    assert a == b == c


def test_kst_has_no_dst_shift():
    # 한국 서머타임은 없다 — 연중 UTC+9 고정
    for m in (1, 3, 7, 10, 11):
        due = r("내일 오후 3시", kst(2026, m, 10, 9))
        assert due.at.astimezone(KST).hour == 15
        assert due.at.hour == 6


# --------------------------------------------------------------------------- 순수성

def test_module_is_pure():
    src = Path(due_parse.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    for bad in (r"\.now\(", r"\.today\(", r"utcnow", r"time\.time", r"\bopen\(", r"import (os|sys|socket|logging|sqlite3|requests|pathlib|random)",
                r"print\("):
        assert not re.search(bad, code), bad
    assert "pa.private" not in src and "counselor" not in src


def test_resolution_does_not_depend_on_wall_clock(monkeypatch):
    a = r("다음 주 월요일 오전 9시")
    monkeypatch.setattr(time, "time", lambda: 0.0)
    assert r("다음 주 월요일 오전 9시") == a


# --------------------------------------------------------------------------- 길이·ReDoS

def timed(fn, limit=1.0):
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    assert dt < limit, f"{dt:.2f}s"
    return out


def test_long_expr_rejected_fast():
    assert timed(lambda: r("내일 " * 1_000_000)) is None
    assert timed(lambda: r("가" * 5_000_000)) is None
    assert r("내일 3시" + " " * 300) is None  # 길이 상한(200자) 초과


def test_long_message_rejected_fast_and_phrase_near_limit_ok():
    assert timed(lambda: r("내일", message="가" * 2_000_000)) is None
    msg = "가" * 19_000 + " 내일 3시에 봐요"
    assert timed(lambda: r("내일 3시", message=msg)) is not None


@pytest.mark.parametrize("evil", [
    "1" * 190, "3/" * 95, "3.1" * 60, "오전 " * 60, "(월)" * 60, "월요일 " * 28, "이번 주 " * 25,
    "1시 " * 60, "내일" * 95, "10:" * 60, "2026-" * 35, "\u200b" * 190, "시" * 190, "3 " * 95, "다음 " * 60,
    "내일 3시~" * 25, "~" * 190, "오후 " + "1" * 150 + "시", "1" * 90 + "일" + "1" * 90 + "월",
])
def test_redos_adversarial_inputs_fast(evil):
    timed(lambda: [r(evil) for _ in range(50)], limit=1.5)
    timed(lambda: r(evil, message=evil + " 내일"), limit=1.0)


# --------------------------------------------------------------------------- 속성 기반

RNG = random.Random(20261006)


def rand_base():
    d = date(2024, 1, 1) + timedelta(days=RNG.randrange(0, 365 * 7))
    h, mi = RNG.randrange(0, 24), RNG.randrange(0, 60)
    tz = RNG.choice([KST, UTC, ZoneInfo("America/New_York"), ZoneInfo("Europe/London")])
    # 임의 tz 의 로컬 시각으로 만들어도 KST 날짜 기준 계산이어야 한다
    return datetime(d.year, d.month, d.day, h, mi, tzinfo=tz)


BASES = [rand_base() for _ in range(400)]


def kdate(at):
    return at.astimezone(KST).date()


def test_prop_relative_days():
    for b in BASES:
        t = kdate(b)
        for word, n in (("오늘", 0), ("내일", 1), ("모레", 2), ("글피", 3)):
            due = r(word, b)
            want = t + timedelta(days=n)
            assert due.all_day and due.at == kst(want.year, want.month, want.day), (b, word)


def test_prop_relative_days_monotonic():
    ordered = sorted(BASES)
    prev = None
    for b in ordered:
        due = r("내일", b)
        if prev is not None:
            assert prev <= due.at
        prev = due.at
    # 시각 포함 표현도 기준이 늦어질수록 마감이 앞서지 않는다
    prev = None
    for b in ordered:
        due = r("내일 오후 3시", b)
        if prev is not None:
            assert prev <= due.at
        prev = due.at


def test_prop_bare_weekday_next_occurrence():
    for b in BASES:
        t = kdate(b)
        for wd in range(7):
            due = r(f"{WD[wd]}요일", b)
            if t.weekday() == wd:
                assert due is None
                continue
            got = kdate(due.at)
            assert 1 <= (got - t).days <= 6
            assert got.weekday() == wd
            # 가장 가까운 그 요일이다 (그 사이에 같은 요일이 없다)
            assert all((t + timedelta(days=k)).weekday() != wd for k in range(1, (got - t).days))


def test_prop_this_week_and_next_week():
    for b in BASES:
        t = kdate(b)
        monday = t - timedelta(days=t.weekday())
        for wd in range(7):
            this = r(f"이번 주 {WD[wd]}요일", b)
            if wd < t.weekday():
                assert this is None
            else:
                g = kdate(this.at)
                assert g == monday + timedelta(days=wd) and g.isocalendar()[:2] == t.isocalendar()[:2]
            nxt = r(f"다음 주 {WD[wd]}요일", b)
            g = kdate(nxt.at)
            assert g == monday + timedelta(days=7 + wd)
            assert g > t
            assert g.isocalendar()[:2] != t.isocalendar()[:2]


def test_prop_month_day_within_window():
    for b in BASES:
        t = kdate(b)
        k = RNG.randrange(0, 61)
        tgt = t + timedelta(days=k)
        for fmt in ("{m}/{d}", "{m}.{d}", "{m}월 {d}일", "{m}월{d}일"):
            due = r(fmt.format(m=tgt.month, d=tgt.day), b)
            assert due is not None and kdate(due.at) == tgt and due.all_day, (b, tgt, fmt)
        # ISO 형식은 연도가 명시되므로 거리 제한 없이 그대로
        far = t + timedelta(days=RNG.randrange(0, 2000))
        due = r(far.isoformat(), b)
        assert due is not None and kdate(due.at) == far


def test_prop_past_month_day_never_resolves_to_past_or_far_future():
    for b in BASES:
        t = kdate(b)
        for k in (1, 7, 30, 100, 200, 364):
            tgt = t - timedelta(days=k)
            due = r(f"{tgt.month}/{tgt.day}", b)
            if due is None:
                continue
            g = kdate(due.at)
            assert g >= t
            assert g.year == t.year or (g - t).days <= 60


def test_prop_time_only():
    for b in BASES:
        kb = b.astimezone(KST)
        for hh in (5, 9, 13, 17, 21, 23):
            mm = RNG.randrange(0, 60)
            due = r(f"{hh:02d}:{mm:02d}" if hh < 7 else f"{hh}:{mm:02d}", b)
            cand = kst(kb.year, kb.month, kb.day, hh, mm)
            if cand > kb:
                assert due is not None and due.at == cand and not due.all_day
            else:
                assert due is None


def test_prop_result_never_in_past():
    exprs = ["오늘", "내일 3시", "모레 오전 9시", "금요일", "이번 주 금요일 15:00", "다음 주 월요일", "10/8", "3일", "12월 25일 오후 6시", "5시", "정오", "자정"]
    for b in BASES:
        for e in exprs:
            due = r(e, b)
            if due is None:
                continue
            kb = b.astimezone(KST)
            assert kdate(due.at) >= kb.date()
            if not due.all_day:
                assert due.at > b
            assert due.at.utcoffset() == timedelta(0)


def test_prop_hour_conversion_monotonic():
    # 같은 날 안에서 시(오전→오후 순)가 커지면 마감도 커진다 (D7 포함, 자정 제외)
    for b in BASES[:50]:
        seq = [r(f"모레 {h}시", b) for h in (7, 8, 9, 10, 11, 12, 1, 2, 3, 4, 5, 6)]
        ats = [d.at for d in seq]
        assert ats == sorted(ats)
        assert len(set(ats)) == 12


def test_prop_random_token_soup_never_raises_and_stays_sane():
    toks = ["내일", "모레", "오전", "오후", "3시", "15:30", "반", "10/8", "금요일", "이번 주", "다음 주", "~", "(금)", "어제", "나중에",
            "0", "99", "시", "일", "월", "/", ".", ":", "\u200b", "３", "정오", "자정", "밤", "새벽", "까지", "에", "3월 1일", "2026-10-08"]
    rng = random.Random(7)
    for _ in range(4000):
        e = " ".join(rng.choice(toks) for _ in range(rng.randrange(1, 7)))
        b = rng.choice(BASES)
        due = r(e, b)
        if due is None:
            continue
        assert isinstance(due, Due) and due.at.utcoffset() == timedelta(0)
        assert kdate(due.at) >= kdate(b)
        if not due.all_day:
            assert due.at > b
        # 같은 입력은 같은 출력 (결정성)
        assert r(e, b) == due

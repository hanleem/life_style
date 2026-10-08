from datetime import date

import pytest

from pa.voice import verbal


@pytest.mark.parametrize("n,want", [(1, "한 건"), (2, "두 건"), (3, "세 건"), (4, "네 건"), (10, "열 건"),
                                    (11, "열한 건"), (20, "스무 건"), (21, "스물한 건"), (99, "아흔아홉 건"), (100, "백 건")])
def test_count_native_counter(n, want):
    assert verbal.count(n) == want


def test_count_zero_and_unit():
    assert verbal.count(0) == "영 건"
    assert verbal.count(3, "개") == "세 개"


@pytest.mark.parametrize("n,want", [(0, "영"), (10, "십"), (16, "십육"), (100, "백"), (101, "백일"), (2026, "이천이십육"),
                                    (9999, "구천구백구십구"), (10000, "만"), (12345, "만이천삼백사십오")])
def test_sino(n, want):
    assert verbal.sino(n) == want


@pytest.mark.parametrize("n,want", [(5, "다섯"), (12, "열두"), (30, "서른"), (40, "마흔"), (55, "쉰다섯")])
def test_native(n, want):
    assert verbal.native(n) == want


@pytest.mark.parametrize("d,want", [(date(2026, 10, 6), "시월 육일 화요일"), (date(2026, 6, 1), "유월 일일 월요일"),
                                    (date(2026, 12, 25), "십이월 이십오일 금요일")])
def test_date_ko(d, want):
    assert verbal.date_ko(d) == want


def test_weekday_ko():
    assert verbal.weekday_ko(date(2026, 10, 6)) == "화요일"


@pytest.mark.parametrize("t,want", [("09:30", "오전 아홉 시 삼십 분"), ("14:00", "오후 두 시"), ("12:10", "낮 열두 시 십 분"),
                                    ("18:30", "저녁 여섯 시 삼십 분"), ("00:00", "밤 열두 시"), ("bad", ""),
                                    ("25:00", ""), ("10:61", ""), ("", ""), ("9:05", "오전 아홉 시 오 분")])
def test_time_ko(t, want):
    assert verbal.time_ko(t) == want


def test_time_ko_non_string():
    assert verbal.time_ko(None) == ""


def test_no_ascii_digits_in_verbalized_output():
    outs = [verbal.count(n) for n in range(0, 120)] + [verbal.time_ko(f"{h:02d}:{m:02d}") for h in range(24) for m in (0, 5, 30)]
    assert not any(ch.isdigit() for o in outs for ch in o)


def test_digits_in_text_are_read():
    assert verbal.speak_digits("3상 모터 2026-10-06 회의") == "삼상 모터 시월 육일 화요일 회의"
    assert verbal.speak_digits("10시 30분") == "열 시 삼십분"
    assert verbal.speak_digits("계좌 1234567890123") == "계좌 일이삼사오육칠팔구영일이삼"
    assert not any(c.isdigit() for c in verbal.speak_digits("v2.5 / 3.14 / 12/25"))


def test_clock_times_in_titles_are_read_as_times():
    assert verbal.speak_digits("14:30 보고") == "오후 두 시 삼십 분 보고"
    assert verbal.speak_digits("99:99") == "구구 구구"


@pytest.mark.parametrize("raw,want", [
    ("10~12시", "열 시에서 열두 시"), ("3~5월", "삼에서 오월"), ("6~10월", "유월에서 시월"), ("7~9", "칠에서 구"),
    ("2026.10.07 마감", "시월 칠일 수요일 마감"), ("2026/10/07", "시월 칠일 수요일"), ("2026-10-07", "시월 칠일 수요일"),
    ("12/25", "십이월 이십오일"), ("6/6 회의", "유월 육일 회의"),
    ("6월 6일", "유월 육일"), ("10월 10일 회의", "시월 십일 회의"), ("3월 3일", "삼월 삼일"), ("12월", "십이월"),
    ("1,000,000원", "백만 원"), ("3,500원", "삼천오백 원"),
    ("3:00 PM 미팅", "오후 세 시 미팅"), ("9 am", "오전 아홉 시"), ("12:30 PM", "낮 열두 시 삼십 분"), ("12 AM", "밤 열두 시"),
    ("2명", "두 명"), ("20명", "스무 명"), ("3개", "세 개"), ("1번", "한 번"), ("5잔", "다섯 잔"), ("24시간", "스물네 시간"),
    ("3시에 회의", "세 시에 회의"), ("100명", "백 명"),
    ("50% 완료", "오십 퍼센트 완료"), ("-5도", "마이너스 오도"), ("A4 출력", "A 포 출력"), ("BK21 보고", "BK 투 원 보고"),
])
def test_verbalizer_gaps(raw, want):
    assert verbal.speak_digits(raw) == want


def test_no_ascii_digit_survives_hostile_numbers():
    for raw in ["~5", "5~", "1~2~3", "0001/0002", "99/99", "2026-13-45", "2026.99.99", "13 PM", "00:61", "1,2,3", "1,234,567,890,123",
                "-0", "−7", "٣", "0명", "000명", "123456789012명", "v1.2.3", "10.10.10.10", "%%50%", "99999999999999999999월"]:
        out = verbal.speak_digits(raw)
        assert not any(c.isascii() and c.isdigit() for c in out), (raw, out)

"""제목 낭독 기본 꺼짐, 가림 재현율, 낭독기·정리 보강, 예산 보정."""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from pa.core import coach_copy
from pa.voice import script, verbal, voice_copy as vc
from pa.voice.script import ScriptOptions, build_script

D = date(2026, 10, 6)
KST = ZoneInfo("Asia/Seoul")
ON = {"say_todo_titles": True, "say_event_titles": True}


def opts(**kw):
    return ScriptOptions(**{**ScriptOptions.default().__dict__, **kw})


def sec(**kw):
    base = {"today_must": [], "rest_count": 0, "calendar": [], "calendar_note": None, "todo_error": None,
            "approvals": 0, "supply": None,
            "done_today": [], "left_today": [], "done_week": 0, "retro": ""}
    base.update(kw)
    return base


def ev(title, time_="10:00", all_day=False, start="2026-10-06T01:00:00+00:00"):
    return {"title": title, "time": time_, "all_day": all_day, "start": start}


# --- 기본값: 제목은 읽지 않는다 ---------------------------------------------------------------------
def test_defaults_are_titles_off():
    o = ScriptOptions.default()
    assert o.say_todo_titles is False and o.say_event_titles is False
    assert o.say_supply is False


@pytest.mark.parametrize("kind", ["morning", "evening", "friday"])
def test_titles_off_has_no_title_text_but_keeps_counts_and_times(kind):
    s = sec(today_must=[{"text": "홍길동 면담 준비", "date": "x"}, {"text": "예산 확인", "date": "x"}], rest_count=2,
            left_today=[{"text": "예산 확인"}, {"text": "비밀 프로젝트"}], done_today=[{"text": "끝낸 일"}],
            calendar=[ev("홍길동 면담", "15:00"), ev("종일 행사", "", True), ev("세미나", "09:30"), ev("넷째", "11:00")])
    r = build_script(kind, D, s, opts=ScriptOptions.default())
    for secret in ("홍길동", "예산", "비밀", "면담", "세미나", "행사", "끝낸", "넷째"):
        assert secret not in r.text, (kind, secret, r.text)
    if kind == "evening":
        assert vc.LEFT_HEAD.format(n="두 건") in r.sentences
    else:
        assert vc.MUST_HEAD.format(n="두 건") in r.sentences
        assert vc.CAL_HEAD["friday" if kind == "friday" else "morning"].format(n="네 건") in r.sentences
        pre = "화요일 " if kind == "friday" else ""
        assert vc.CAL_TIME.format(when=pre + "오후 세 시") in r.sentences
        assert vc.CAL_TIME.format(when=pre + "오전 아홉 시 삼십 분") in r.sentences
        assert (vc.CAL_ALLDAY_ONLY.format(when="화요일") if kind == "friday" else vc.CAL_ALLDAY_PLAIN) in r.sentences
    assert coach_copy.violations(r.text) == []


def test_titles_off_example_sentences():
    r = build_script("morning", D, sec(calendar=[ev("무엇이든", "15:00")], today_must=[{"text": "a"}] * 5), opts=ScriptOptions.default())
    assert "오후 세 시에 일정이 있습니다." in r.sentences
    assert vc.MUST_HEAD.format(n="다섯 건") in r.sentences        # '오늘 꼭 할 일은 다섯 건입니다.'


def test_titles_off_rest_count_still_spoken():
    r = build_script("morning", D, sec(today_must=[{"text": "a"}] * 3, rest_count=4), opts=ScriptOptions.default())
    assert vc.REST.format(n="네 건") in r.sentences


def test_titles_off_short_mode_no_event_lines():
    r = build_script("morning", D, sec(calendar=[ev("x")]), opts=opts(short=True))
    assert not any("에 일정이 있습니다" in x for x in r.sentences)


def test_titles_off_all_day_in_friday_and_morning():
    r = build_script("morning", D, sec(calendar=[ev("행사", "", True)]), opts=ScriptOptions.default())
    assert vc.CAL_ALLDAY_PLAIN in r.sentences
    r = build_script("friday", D, sec(calendar=[ev("행사", "", True, "2026-10-12T00:00:00+00:00")]), opts=ScriptOptions.default())
    assert vc.CAL_ALLDAY_ONLY.format(when="월요일") in r.sentences


# --- 켠 경우: 가려진 항목은 개수 한 문장, 시각도 말하지 않는다 -----------------------------------------
def test_masked_events_folded_into_one_count_and_time_dropped():
    s = sec(calendar=[ev("가족 회의", "10:00"), ev("치과 예약", "14:00"), ev("정신과", "16:20"), ev("세미나", "17:00")])
    r = build_script("morning", D, s, opts=opts(**ON))
    assert vc.PRIVATE_EVENTS.format(n="두 건") in r.sentences
    assert sum(1 for x in r.sentences if "개인 일정" in x) == 1
    for leaked in ("치과", "정신과", "오후 두 시", "오후 네 시", "사십 분", "이십 분"):
        assert leaked not in r.text, leaked
    assert "오전 열 시, 가족 회의." in r.sentences and "오후 다섯 시, 세미나." in r.sentences


def test_masked_todos_folded_into_one_count():
    s = sec(today_must=[{"text": "병원 예약", "date": "x"}, {"text": "치과", "date": "x"}, {"text": "논문 초안", "date": "x"}])
    r = build_script("morning", D, s, opts=opts(**ON))
    assert vc.PRIVATE_TODOS.format(n="두 건") in r.sentences
    assert sum(1 for x in r.sentences if "개인 항목" in x) == 1
    assert "병원" not in r.text and "치과" not in r.text and "논문 초안" in r.text
    e = build_script("evening", D, sec(left_today=[{"text": "병원"}, {"text": "치과"}]), opts=opts(**ON))
    assert vc.PRIVATE_TODOS.format(n="두 건") in e.sentences


# --- 비문자열 값 ------------------------------------------------------------------------------------------
def test_non_string_values_are_never_stringified():
    bad = [{"x": "비밀딕트"}, ["비밀리스트"], None, 12345, 3.5, True, ("비밀튜플",), b"bytes"]
    s = sec(today_must=[{"text": v, "date": "x"} for v in bad] + [{"text": "정상 작업", "date": "x"}],
            left_today=[{"text": v} for v in bad], calendar=[ev(v) for v in bad],
            )
    s["retro"] = ["리스트"]
    for kind in ("morning", "evening", "friday"):
        r = build_script(kind, D, s, opts=opts(must_cap=9, **ON),
                         friday=s if kind == "morning" else None)
        for frag in ("비밀", "bytes", "12345", "삼 점", "참", "None", "{", "[", "x"):
            assert frag not in r.text, (kind, frag, r.text)
        assert "정상 작업" in r.text or kind == "evening"
        assert not any(c.isdigit() for c in r.text)


def test_non_dict_inputs_do_not_crash():
    for secs in (None, [], "x", 3, {"today_must": "x", "calendar": {"a": 1}, "supply": "s"}):
        r = build_script("morning", D, secs, opts=opts(**ON, say_supply=True), friday="bad")
        assert r.sentences[-1] == vc.CLOSE
    assert script._spoken({"a": 1}) is None and script._spoken(None) is None and script._spoken(5) is None


# --- 이메일·URL·~·마크다운 -----------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,gone", [("kim@example.com 회신 요청", "example"), ("www.foo.com 확인", "foo"), ("https://a.b/c?d=1 열람", "a.b"),
                                      ("http://x.y 확인 @minju", "minju"), ("h​ttps://evil.example/x 확인", "evil")])
def test_emails_and_urls_are_not_spoken(raw, gone):
    r = build_script("morning", D, sec(today_must=[{"text": raw, "date": "x"}], calendar=[ev(raw)]), opts=opts(**ON))
    assert gone not in r.text and "kim" not in r.text and "@" not in r.text and "www" not in r.text
    assert "확인" in r.text or "회신" in r.text or "열람" in r.text


def test_tilde_handled_before_markdown():
    assert script._spoken("10~12시 회의") == "열 시에서 열두 시 회의"
    assert script._spoken("3~5월 휴가철 점검") == "삼에서 오월 휴가철 점검"
    assert script._spoken("6~10월 일정") == "유월에서 시월 일정"
    assert "천십이" not in script._spoken("10~12시 회의")
    assert script.clean_title("~~취소~~ 회의") == "취소 회의"
    assert "~" not in script._spoken("~5건 처리")
    r = build_script("morning", D, sec(calendar=[ev("10~12시 점검")]), opts=opts(**ON))
    assert "열 시에서 열두 시 점검" in r.text and not any(c.isdigit() for c in r.text)


def test_months_inside_titles_use_exception_words():
    assert script._spoken("6월 보고서") == "유월 보고서" and script._spoken("10월 정산") == "시월 정산"


def test_ordinals_and_rounds_are_masked_and_counted():
    """'1차 심사'·'11차 회의'·'3번째 미팅'은 coach_copy 의 연속 기록 표현('일차'·'째')과 겹쳐 읽지 않는다(허용된 과가림).
    개수 문장으로만 센다."""
    s = sec(today_must=[{"text": t, "date": "x"} for t in ("1차 심사", "11차 회의", "3번째 미팅")])
    r = build_script("morning", D, s, opts=opts(**ON))
    assert vc.PRIVATE_TODOS.format(n="세 건") in r.sentences
    assert "심사" not in r.text and "미팅" not in r.text and coach_copy.violations(r.text) == []


def test_a4_is_not_read_as_digit_four():
    assert script._spoken("A4 출력") == "A 포 출력"
    assert "에이사" not in script._spoken("A4 출력")


# --- 예산 보정 ------------------------------------------------------------------------------------------------
def test_titles_not_shortened_when_only_sentence_count_exceeds():
    s = sec(today_must=[{"text": "열다섯글자를넘기는긴제목입니다확인"}] * 2, rest_count=3, calendar=[ev("회의")] * 6, approvals=1,
            supply={"items": [{"due": True}]})
    r = build_script("morning", D, s, opts=opts(**ON, say_supply=True))
    assert r.dropped >= 1 and "열다섯글자를넘기는긴제목입니다확인" in r.text


def test_dropped_counts_rest_sentence_removal():
    s = sec(today_must=[{"text": "가나다라마바사아자차카타파하가나다라마바사아자차"}] * 3, rest_count=5, approvals=3)
    r = build_script("morning", D, s, opts=opts(**ON, short=True, must_cap=3))
    base = build_script("morning", D, s, opts=opts(**ON, must_cap=3))
    assert len(r.text.replace(" ", "")) <= script.MAX_CHARS_SHORT
    assert r.dropped > base.dropped


def test_shortening_only_when_chars_exceed():
    # 문장 수만 넘고 글자는 여유 — 제목 길이를 줄이지 않는다
    s = sec(today_must=[{"text": "열여섯글자제목입니다입니다입니다"}] * 3, approvals=1, calendar=[ev("회의")] * 3,
            mail={"reply_count": 1, "titles": [], "error": None})
    r = build_script("morning", D, s, opts=opts(**ON, must_cap=3))
    assert len(r.text.replace(" ", "")) <= script.MAX_CHARS
    assert all("열여섯글자제목입니다입니다입니다" in x for x in r.sentences if x.startswith(("먼저", "다음으로", "그리고")))


# --- 가림 재현율 표(리뷰어 프로브 103건) ----------------------------------------------------------------------
# (제목, 가려야 하면 1)
TITLES = [
    ("가족 회의", 0), ("상담 일정", 0), ("건강검진", 1), ("병원 예약", 1), ("치과", 1), ("금주 모임", 1), ("저녁 약속", 0),
    ("심리상담", 1), ("불면 클리닉", 1), ("이혼 상담", 1), ("면접 평가", 0), ("연구비 정산", 0), ("의사결정 회의", 0),
    ("NMR 분석", 0), ("박사과정 논문 심사", 0), ("학과 회의", 0), ("세미나 발표 준비", 0), ("과제 보고서 제출", 0),
    ("치과 예약", 1), ("치 과", 1), ("정신건강의학과", 1), ("신경정신과 진료", 1), ("피검사", 1), ("혈액검사", 1),
    ("내시경", 1), ("위내시경 예약", 1), ("MRI 촬영", 1), ("수술 상담", 1), ("물리치료", 1), ("재활", 1), ("한방병원", 1),
    ("안과", 1), ("피부과", 1), ("이비인후과", 1), ("정형외과", 1), ("비뇨기과", 1), ("산부인과 검진", 1), ("약국 들르기", 1),
    ("약을 타러", 1), ("처방전 받기", 1), ("AA meeting", 1), ("dentist", 1), ("therapy", 1), ("psychiatrist", 1),
    ("blood test", 1), ("doctor appt", 1), ("counseling", 1), ("변호사 상담", 1), ("소송 준비", 1), ("법원 출석", 1),
    ("이혼 조정", 1), ("대출 상환", 1), ("카드값 연체", 1), ("세무 상담", 1), ("부부 상담", 1), ("데이트", 1),
    ("소개팅", 1), ("장례식장", 1), ("문상", 1), ("금주 3주차 점검", 1), ("단주 모임", 1), ("AA 모임", 1),
    ("명상 앱", 1), ("정신과약 리필", 1), ("우울증 검사", 1), ("공황장애", 1), ("ADHD 검사", 1), ("상 담 예약", 1),
    ("병원에서 검사", 1), ("병 원", 1), ("수 면", 1), ("음  주", 1), ("치과에", 1), ("술자리", 1), ("술약속", 1),
    ("소주 한잔", 1), ("회식", 0), ("잠깐 통화", 0), ("약 30분 회의", 0), ("요약 발표", 0), ("의사 면담", 1),
    ("다음 주 월요일 보고", 0), ("2차 병원", 1), ("PT 받기", 1), ("헬스", 1), ("체지방 측정", 1), ("혈압 측정", 1),
    ("인바디", 1), ("임플란트", 1), ("스케일링", 1), ("안경 맞추기", 0), ("병가", 1), ("휴직 서류", 1),
    ("HIV 검사", 1), ("코로나 검사", 1), ("백신 접종", 1), ("독감 주사", 1), ("상담센터", 1), ("코칭 세션", 1),
    ("면담 (친구 김OO 고민)", 1), ("친구 홍길수 면담", 0), ("Team meeting 10:00", 0), ("동네 공동 세미나", 0),
]
# 문서화된 과가림(안전한 쪽의 비용): '의사'·'상담'·'약' 단어, 그리고 공백을 지운 사본에서 이름 끝 글자와 다음 낱말이 이어 붙는
# 우연('홍길동 면담' → '수면')
ACCEPTED_OVERHIDE = {"의사결정 회의", "상담 일정", "약 30분 회의", "친구 홍길수 면담"}
SHOULD_SHOW = [t for t, hide in TITLES if not hide and t not in ACCEPTED_OVERHIDE]


def _read(title):
    """제목 낭독을 켠 할 일 경로에서 이 제목이 읽히는가(가려지면 None)."""
    r = build_script("morning", D, sec(today_must=[{"text": title, "date": "2026-10-06"}]), opts=opts(**ON))
    item = [x for x in r.sentences if x.startswith("먼저")]
    return item[0] if item else None


def test_recall_table_hide_at_least_95_percent():
    hide = [t for t, h in TITLES if h]
    missed = [t for t in hide if _read(t) is not None]
    recall = 1 - len(missed) / len(hide)
    print(f"\nrecall {len(hide) - len(missed)}/{len(hide)} = {recall:.1%}; missed: {missed}")
    assert recall >= 0.95, missed
    assert len(TITLES) >= 100


def test_should_show_titles_stay_readable():
    wrongly_hidden = [t for t in SHOULD_SHOW if _read(t) is None]
    assert wrongly_hidden == []


def test_documented_overhides_are_still_hidden_not_leaked():
    for t in ACCEPTED_OVERHIDE:
        assert _read(t) is None


@pytest.mark.parametrize("title", ["치 과", "병 원", "수 면", "음  주", "치​과", "ＡＡ meeting"])
def test_split_words_are_hidden(title):
    assert voice_copy_sensitive(title)


def voice_copy_sensitive(t):
    return vc.is_sensitive(t)


def test_next_week_is_not_a_false_positive_but_spaced_drinking_is_hidden():
    assert not vc.is_sensitive("다음 주 발표") and not vc.is_sensitive("다음​주 발표") and not vc.is_sensitive("다음  주")
    assert vc.is_sensitive("음 주") and vc.is_sensitive("금 주")


def test_same_masking_on_event_path():
    for t, hide in TITLES:
        r = build_script("morning", D, sec(calendar=[ev(t)]), opts=opts(**ON))
        shown = any(x.startswith("오전 열 시,") for x in r.sentences)
        if hide:
            assert not shown or t in ("", ), t

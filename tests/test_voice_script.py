from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from pa.core import briefing, coach_copy
from pa.voice import script, voice_copy
from pa.voice.script import ScriptOptions, build_script

D = date(2026, 10, 6)
KST = ZoneInfo("Asia/Seoul")


def sec(**kw):
    base = {"today_must": [], "rest_count": 0, "calendar": [], "calendar_note": None, "todo_error": None,
            "approvals": 0, "supply": None,
            "done_today": [], "left_today": [], "done_week": 0, "retro": ""}
    base.update(kw)
    return base


def todos(*titles):
    return [{"text": t, "date": "2026-10-06"} for t in titles]


def ev(title, time_="10:00", all_day=False):
    return {"title": title, "time": time_, "all_day": all_day, "start": "2026-10-06T01:00:00+00:00"}


TITLES_ON = {"say_todo_titles": True, "say_event_titles": True}


def build(kind="morning", s=None, **opt):
    """제목 낭독을 켠 옵션(기본값은 꺼짐 — 꺼진 경우는 tests/test_voice_defaults.py)."""
    o = ScriptOptions(**{**ScriptOptions.default().__dict__, **TITLES_ON, **opt})
    return build_script(kind, D, s if s is not None else sec(), opts=o)


def test_morning_order_must_then_calendar_then_approvals():
    s = sec(today_must=todos("논문 초안", "예산 확인", "장비 점검"), calendar=[ev("학교 행사", "10:00"), ev("세미나", "14:00")],
            approvals=2)
    out = build(s=s).sentences
    assert out[0] == voice_copy.OPEN["morning"].format(date="시월 육일 화요일")
    assert out[1] == voice_copy.MUST_HEAD.format(n="세 건")
    assert [x.endswith(".") for x in out[2:5]] == [True] * 3
    assert "논문 초안" in out[2] and "예산 확인" in out[3] and "장비 점검" in out[4]
    assert out[5] == voice_copy.CAL_HEAD["morning"].format(n="두 건")
    assert out[6] == "오전 열 시, 학교 행사." and out[7] == "오후 두 시, 세미나."
    assert out[8] == voice_copy.APPROVALS.format(n="두 건")
    assert out[-1] == voice_copy.CLOSE


def test_item_prefixes_are_not_ordinals():
    out = build(s=sec(today_must=todos("가", "나", "다"))).sentences
    assert out[2].startswith("먼저") and out[3].startswith("다음으로") and out[4].startswith("그리고")
    assert not any("째" in p for p in voice_copy.ITEM_PREFIXES)


def test_must_cap_respected_and_rest_counted():
    s = sec(today_must=todos("가업무", "나업무", "다업무", "라업무", "마업무"), rest_count=0)
    out = build(s=s, must_cap=3).sentences
    items = [x for x in out if any(k in x for k in ("가업무", "나업무", "다업무", "라업무", "마업무"))]
    assert len(items) == 3
    assert any(x == voice_copy.REST.format(n="두 건") for x in out)


def test_rest_count_from_briefing_added():
    out = build(s=sec(today_must=todos("가", "나", "다"), rest_count=4), must_cap=3).sentences
    assert voice_copy.REST.format(n="네 건") in out


def test_low_capacity_short_mode():
    s = sec(today_must=todos(*[f"작업{c}" for c in "가나다라마"]), calendar=[ev("x미팅")] * 4,
            approvals=1)
    r = build(s=s, short=True, must_cap=2)
    assert len(r.text.replace(" ", "")) <= script.MAX_CHARS_SHORT and len(r.sentences) <= script.MAX_SENTENCES_SHORT
    assert not any(t in r.text for t in ("미팅", "컨디션", "여력", "낮음", "이번 주"))


def test_budget_drops_low_priority_first():
    s = sec(today_must=todos("논문 초안 작성하기", "예산 확인하기", "장비 점검하기"),
            calendar=[ev(f"긴 이름의 회의 번호 {chr(0xAC00 + i)}", "10:00") for i in range(20)], approvals=3,
            rest_count=5, supply={"items": [{"due": True}]})
    r = build(s=s, say_supply=True)
    assert len(r.text.replace(" ", "")) <= script.MAX_CHARS and len(r.sentences) <= script.MAX_SENTENCES
    assert r.sentences[0].endswith("브리핑입니다.") and r.sentences[-1] == voice_copy.CLOSE
    assert any(x.startswith("오늘 꼭 할 일은") for x in r.sentences)
    assert all(t in r.text for t in ("논문 초안", "예산 확인", "장비 점검"))
    assert r.dropped > 0


def test_budget_extreme_long_titles_still_fits():
    s = sec(today_must=todos(*["아주 긴 제목 " * 30] * 3), approvals=4, calendar=[ev("회의")] * 3)
    r = build(s=s)
    assert len(r.text.replace(" ", "")) <= script.MAX_CHARS and len(r.sentences) <= script.MAX_SENTENCES
    r2 = build(s=s, short=True)
    assert len(r2.text.replace(" ", "")) <= script.MAX_CHARS_SHORT


def test_budget_title_retruncation_to_15():
    s = sec(today_must=todos(*["가나다라마바사아자차카타파하" * 2 + "끝"] * 3))   # 29자 x3
    r = build(s=s, short=True)
    assert len(r.text.replace(" ", "")) <= script.MAX_CHARS_SHORT


def test_p2_items_dropped_from_back_and_rest_recomputed():
    s = sec(today_must=todos(*[("할일" + chr(0xAC00 + i)) * 5 for i in range(5)]))   # 10자 x5
    r = build(s=s, must_cap=5, short=True)
    kept = [x for x in r.sentences if x.startswith(("먼저", "다음으로", "그리고", "또"))]
    assert len(r.text.replace(" ", "")) <= script.MAX_CHARS_SHORT
    assert len(kept) < 5
    assert voice_copy.REST.format(n=__import__("pa.voice.verbal", fromlist=["x"]).count(5 - len(kept))) in r.sentences


def test_evening_variant():
    s = sec(done_today=todos("a", "b"), left_today=todos("마무리 정리", "내일 준비"), retro=briefing._RETRO[0], approvals=1)
    r = build("evening", s)
    assert r.sentences[0] == voice_copy.OPEN["evening"].format(date="시월 육일 화요일")
    assert voice_copy.DONE_TODAY.format(n="두 건") in r.sentences
    assert voice_copy.LEFT_HEAD.format(n="두 건") in r.sentences
    assert "마무리 정리" in r.text
    assert voice_copy.RETRO.format(q=briefing._RETRO[0]) in r.sentences
    assert r.sentences[-1] == voice_copy.CLOSE


def test_evening_retro_outside_allowed_dropped():
    r = build("evening", sec(left_today=todos("a"), retro="임의의 문장 이야기해 주세요"))
    assert "임의의 문장" not in r.text and "오늘의 질문" not in r.text


def test_evening_nothing_left():
    r = build("evening", sec())
    assert voice_copy.LEFT_NONE in r.sentences


def test_all_allowed_retros_are_coach_safe():
    for q in voice_copy.RETRO_ALLOWED:
        assert coach_copy.violations(q) == [] and coach_copy.notify_safe(q), q
    assert voice_copy.RETRO_ALLOWED   # 비어 있지 않음


def test_friday_kind_and_friday_tail():
    fsec = sec(done_week=7, today_must=todos("주간 정리"), calendar=[ev("다음 발표", "09:00")])
    fsec["calendar"][0]["start"] = "2026-10-12T00:00:00+00:00"
    r = build("friday", fsec)
    assert r.sentences[0] == voice_copy.OPEN["friday"].format(date="시월 육일 화요일")
    assert voice_copy.WEEK_DONE.format(n="일곱 건") in r.sentences
    assert voice_copy.NEXT_WEEK.format(n="한 건") in r.sentences
    assert any("월요일 오전 아홉 시, 다음 발표" in x for x in r.sentences)
    m = build_script("morning", D, sec(today_must=todos("가")), opts=ScriptOptions(**{**ScriptOptions().__dict__, **TITLES_ON}),
                     friday=fsec)
    assert voice_copy.WEEK_DONE.format(n="일곱 건") in m.sentences
    assert voice_copy.NEXT_WEEK.format(n="한 건") in m.sentences
    assert m.sentences[0].endswith("아침 브리핑입니다.")
    assert build_script("morning", D, sec(), opts=ScriptOptions.default()).text.count("지난 칠 일") == 0


def test_short_mode_skips_friday_tail():
    m = build_script("morning", D, sec(), opts=ScriptOptions(**{**ScriptOptions.default().__dict__, "short": True}),
                     friday=sec(done_week=3))
    assert "지난 칠 일" not in m.text


def test_zero_counts_omit_sentences():
    r = build(s=sec(today_must=todos("가")))
    for frag in ("승인을 기다리는", "주문 확인"):
        assert frag not in r.text


def test_empty_briefing():
    r = build()
    assert r.sentences[0].endswith("브리핑입니다.") and r.sentences[-1] == voice_copy.CLOSE
    assert voice_copy.MUST_NONE in r.sentences and voice_copy.CAL_NONE in r.sentences
    assert r.dropped == 0 and r.est_seconds >= 1


def test_empty_dict_sections_and_none_values():
    r = build_script("morning", D, {}, opts=ScriptOptions.default())
    assert voice_copy.MUST_NONE in r.sentences
    r = build_script("morning", D, {"today_must": None, "calendar": None, "supply": None},
                     opts=ScriptOptions.default())
    assert r.sentences[-1] == voice_copy.CLOSE


def test_only_counts():
    r = build(s=sec(approvals=3))
    assert voice_copy.APPROVALS.format(n="세 건") in r.sentences


def test_errors_are_spoken_plainly():
    s = sec(calendar_note="캘린더를 불러오지 못했습니다", todo_error="할 일을 불러오지 못했습니다")
    out = build(s=s).sentences
    assert voice_copy.CAL_ERR in out and voice_copy.TODO_ERR in out


def test_calendar_not_connected_note_is_not_read_verbatim():
    out = build(s=sec(calendar_note="캘린더 미연결")).sentences
    assert voice_copy.CAL_ERR in out and "미연결" not in " ".join(out)


def test_calendar_items_max_three_and_more():
    s = sec(calendar=[ev(f"회의{c}", "09:00") for c in "가나다라마"])
    out = build(s=s).sentences
    assert sum(1 for x in out if x.startswith("오전 아홉 시,")) == 3
    assert voice_copy.CAL_MORE.format(n="두 건") in out


def test_allday_and_missing_time():
    out = build(s=sec(calendar=[ev("워크숍", "", True), ev("점심 약속 아님", "")])).sentences
    assert "종일, 워크숍." in out
    assert "점심 약속 아님." in out


def test_optional_titles_on():
    s = sec(supply={"items": [{"label": "정기배송", "remaining": 2, "due": True, "hint": None}]})
    r = build(s=s, say_supply=True)
    assert voice_copy.SUPPLY_DUE.format(n="한 건") in r.sentences
    assert "정기배송" not in r.text


def test_titles_off_omit_items():
    out = build(s=sec(today_must=todos("논문")), say_todo_titles=False).sentences
    assert not any("논문" in x for x in out)
    assert voice_copy.MUST_HEAD.format(n="한 건") in out
    out = build(s=sec(calendar=[ev("세미나")]), say_event_titles=False).sentences
    assert not any("세미나" in x for x in out)


def test_hash_stable_and_changes_with_content():
    a = build(s=sec(today_must=todos("가")))
    assert a.script_hash == build(s=sec(today_must=todos("가"))).script_hash and len(a.script_hash) == 16
    assert a.script_hash != build(s=sec(today_must=todos("나"))).script_hash
    assert a.script_hash != build_script("morning", date(2026, 10, 7), sec(today_must=todos("가")),
                                         opts=ScriptOptions.default()).script_hash
    assert a.text == " ".join(a.sentences) and a.kind == "morning" and a.for_date == D


def test_est_seconds_about_one_minute_at_budget():
    s = sec(today_must=todos(*["긴 제목 확인 작업 정리"] * 3), calendar=[ev("회의 번호")] * 3, approvals=2)
    r = build(s=s)
    assert r.est_seconds <= 60 and script.CHARS_PER_SEC == 5.0


def test_pick_kind_rules():
    t = time(17, 30)

    def at(h, m=0):
        return datetime(2026, 10, 6, h, m, tzinfo=KST)
    assert script.pick_kind({"morning"}, at(9), t) == "morning"
    assert script.pick_kind({"morning", "evening"}, at(18), t) == "evening"
    assert script.pick_kind({"morning", "evening"}, at(16), t) == "morning"
    assert script.pick_kind({"friday"}, at(8, 10), t) == "friday"
    assert script.pick_kind(set(), at(9), t) is None
    assert script.pick_kind({"evening"}, at(9), t) == "evening"
    assert script.pick_kind({"morning", "friday"}, at(9), None) == "morning"
    assert script.pick_kind({"morning", "evening"}, at(23), None) == "morning"
    assert script.pick_kind({"friday", "evening"}, at(18), t) == "evening"


def test_clean_title_strips_markup():
    assert script.clean_title("게이트 승인 https://x.y/z 🔴 **긴급**") == "게이트 승인 긴급"
    assert script.clean_title("할 일 #123 (#45)") == "할 일"
    assert script.clean_title("`코드` > ~인용~ _강조_") == "코드 인용 강조"
    assert script.clean_title("🔴🔴") == ""


def test_clean_title_truncates_at_last_space():
    full = " ".join(["가나다", "라마바", "사아자", "차카타", "파하가", "나다라", "마바사", "아자차", "카타파", "하가나"])
    t = script.clean_title(full)
    assert len(t) <= script.TITLE_MAX and not t.endswith(" ") and "…" not in t
    assert full.startswith(t) and full[len(t)] == " "
    assert len(script.clean_title("가" * 100)) == script.TITLE_MAX


def test_titles_with_digits_and_dates_have_no_ascii_digits():
    s = sec(today_must=todos("3상 모터 2026-10-06 점검", "v2.5 배포 12/25"), calendar=[ev("2차 리뷰 10:30", "09:00")])
    r = build(s=s)
    assert not any(c.isdigit() for c in r.text)
    assert "삼상 모터" in r.text


def test_script_version_constants():
    assert script.SCRIPT_VERSION == "v1" and (script.MAX_CHARS, script.MAX_SENTENCES) == (300, 12)
    assert (script.MAX_CHARS_SHORT, script.MAX_SENTENCES_SHORT, script.TITLE_MAX) == (180, 7, 30)
    assert "mood" not in script.ALLOWED_SECTIONS and not {"lab", "mail"} & set(script.ALLOWED_SECTIONS)


# --- 출처별 낭독 설정(today_must/left_today 항목의 source·origin 태그) ---
_SRC_CASES = [  # (source, origin, 켜야 할 설정 또는 None=설정 없음, "never"=항상 침묵)
    ("task", ["task"], None),
    ("event", ["event"], "say_event_titles"),
    ("approval", ["approval"], "never"),
]


def _off_all(**on):
    return ScriptOptions(**{**ScriptOptions.default().__dict__, "say_todo_titles": True, "say_event_titles": False, **on})


@pytest.mark.parametrize("kind,field", [("morning", "today_must"), ("evening", "left_today")])
@pytest.mark.parametrize("src,origin,gate", _SRC_CASES)
def test_item_source_gated_by_matching_setting(kind, field, src, origin, gate):
    s = sec(**{field: [{"text": "비밀제목XYZ", "date": "2026-10-06", "source": src, "origin": origin}]})
    off = " ".join(build_script(kind, D, s, opts=_off_all()).sentences)
    if gate is None:
        assert "비밀제목XYZ" in off
        return
    assert "비밀제목XYZ" not in off
    on_opts = _off_all(**({gate: True} if gate != "never" else {"say_event_titles": True}))
    on = " ".join(build_script(kind, D, s, opts=on_opts).sentences)
    assert ("비밀제목XYZ" in on) == (gate != "never")


def test_item_titles_off_hides_task_source_even_with_other_settings_on():
    s = sec(today_must=[{"text": "비밀제목XYZ", "source": "task", "origin": ["task"]}])
    o = ScriptOptions(**{**ScriptOptions.default().__dict__, "say_todo_titles": False, "say_event_titles": True})
    assert "비밀제목XYZ" not in " ".join(build_script("morning", D, s, opts=o).sentences)

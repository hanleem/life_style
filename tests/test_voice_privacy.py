import re
import unicodedata
from datetime import date, timedelta

import pytest

from pa.core import coach_copy
from pa.voice import script, voice_copy

D = date(2026, 10, 6)
PRIVATE_FIXTURE = {
    "journal": "오늘 너무 지쳤다 비밀일기", "sleep": {"awakenings": 4}, "alcohol": "많이 마심", "mood": "low",
    "counsel": "상담 내용 원문", "taper": "감주 2단계", "weekly_retro": "이번 주 회고 원문",
    "capacity": {"capacity": "low", "constraints": ["오후 회의 줄이기"]},
}


def _sections(**kw):
    base = {"today_must": [], "rest_count": 0, "calendar": [], "calendar_note": None, "todo_error": None,
            "approvals": 0, "supply": None,
            "done_today": [], "left_today": [], "done_week": 0, "retro": ""}
    base.update(kw)
    return base


TITLES_ON = {"say_todo_titles": True, "say_event_titles": True}


def _build(sec, kind="morning", **opt):
    """제목 낭독을 켠 옵션으로 만든다(기본은 꺼짐 — 꺼진 경우는 tests/test_voice_defaults.py)."""
    o = script.ScriptOptions.default()
    opt = {**TITLES_ON, **opt}
    return script.build_script(kind, D, sec, opts=script.ScriptOptions(**{**o.__dict__, **opt}))


def test_every_template_passes_coach_checks():
    for t in voice_copy.ALL_TEMPLATES + tuple(voice_copy.UI.values()):
        assert coach_copy.violations(t) == [], t
        assert coach_copy.notify_safe(t), t


def test_all_templates_covers_dict_values():
    for v in (*voice_copy.OPEN.values(), *voice_copy.CAL_HEAD.values(), voice_copy.CLOSE, voice_copy.LEFT_NONE,
              *voice_copy.ITEM_PREFIXES):
        assert v in voice_copy.ALL_TEMPLATES


def test_unknown_sections_are_never_read():
    s = _build({**_sections(), **PRIVATE_FIXTURE})
    for v in ("비밀일기", "많이 마심", "상담 내용", "감주", "회고 원문", "오후 회의 줄이기", "low"):
        assert v not in s.text


def test_unknown_sections_even_with_known_shaped_values_not_read():
    sec = _sections(capacity_signal={"capacity": "low", "text": "컨디션 낮음"}, body_md="> 요약 줄", sleep_note="잠 부족")
    r = _build(sec)
    assert "컨디션" not in r.text and "요약" not in r.text and "잠" not in r.text


def test_allowlist_filter_function():
    kept = script._allowed({"today_must": [1], "mood": "x", "capacity_signal": 1, "body_md": "z"})
    assert set(kept) == {"today_must"}


@pytest.mark.parametrize("title", ["병원 예약", "정신과 상담", "술 약속", "수면 클리닉", "약 먹기", "체중 재기",
                                   "치과 진료", "일기 쓰기", "감주 계획 확인"])
def test_sensitive_titles_masked(title):
    sec = _sections(today_must=[{"text": title, "date": "2026-10-06"}],
                    calendar=[{"title": title, "time": "10:00", "all_day": False}],
                    left_today=[{"text": title}])
    for kind in ("morning", "evening"):
        s = _build(sec, kind)
        assert title not in s.text and voice_copy.PRIVATE_ITEM in s.text


@pytest.mark.parametrize("title", ["학술대회 발표", "기술 검토", "계약서 서명", "약속 장소 정하기", "요약 보고서", "잠정 결과 공유",
                                   "다음 주 발표 준비"])
def test_common_work_titles_not_masked(title):
    sec = _sections(today_must=[{"text": title, "date": "2026-10-06"}])
    assert title in _build(sec).text


@pytest.mark.parametrize("title", [
    "술​약속",            # 제로폭 끼워 넣기
    "ㅤ음‍주 모임",
    "ＳＵＬ 약 먹기",           # 전각 + 약
    "６０ｋｇ 달성",            # 전각 숫자·kg
    "수­면 관리",          # soft hyphen
    "상**담** 예약",            # 마크다운으로 쪼갠 단어
    "㈜병원 방문",
    "정신과‮",
    "BMI 체중 기록",
    "Heavy 음주 기록",
])
def test_unicode_smuggling_is_masked(title):
    s = _build(_sections(today_must=[{"text": title, "date": "2026-10-06"}]))
    assert voice_copy.PRIVATE_ITEM in s.text
    for frag in ("술", "음주", "수면", "상담", "병원", "체중", "kg", "ｋｇ", "약 먹"):
        assert frag not in s.text.replace("개인 항목", "")


def test_is_sensitive_unit():
    assert voice_copy.is_sensitive("술 약속") and voice_copy.is_sensitive("약 먹기") and voice_copy.is_sensitive("70kg")
    assert voice_copy.is_sensitive("70 KG") and voice_copy.is_sensitive("잠 부족")
    assert not voice_copy.is_sensitive("다음 주 발표") and not voice_copy.is_sensitive("학술")
    assert not voice_copy.is_sensitive("")


def test_sensitive_terms_exclude_single_chars_and_cover_plan():
    for single in ("술", "잠", "약", "kg"):
        assert single not in voice_copy.SENSITIVE_TERMS
    for w in ("수면제", "정신과", "상담", "감주", "일기", "회고", "산부인과", "한의원", "음주", "건강"):
        assert w in voice_copy.SENSITIVE_TERMS
    for w in coach_copy.FORBIDDEN_NOTIFY_WORDS:
        assert w in voice_copy.SENSITIVE_TERMS or w in ("술", "잠", "약", "kg"), w


def test_titles_failing_coach_violations_are_masked():
    r = _build(_sections(today_must=[{"text": "연속 측정 결과 정리", "date": "x"}]))
    assert "연속" not in r.text and voice_copy.PRIVATE_ITEM in r.text


def test_supply_off_by_default():
    sec = _sections(supply={"items": [{"label": "샐러드 정기배송", "remaining": 2, "due": True, "hint": None}]})
    t = _build(sec).text
    assert "샐러드" not in t and "끼니" not in t and "주문" not in t


def test_supply_on_is_counts_only_and_masks():
    sec = _sections(supply={"items": [{"label": "술안주 세트", "remaining": 2, "due": True, "hint": "오늘 19:00 전"},
                                      {"label": "x", "remaining": 1, "due": False, "hint": None}]})
    t = _build(sec, say_supply=True).text
    assert "술안주" not in t and "19" not in t and "주문 확인이 필요한 항목이 한 건 있습니다." in t






def test_no_digits_markdown_or_emoji_in_script():
    sec = _sections(
        today_must=[{"text": "[알림] 3월 12일 14:30 보고서 #12 **제출** 🔴", "date": "2026-10-06"}],
        calendar=[{"title": "2026-10-07 회의 v2", "time": "09:05", "all_day": False}, {"title": "워크숍", "time": "", "all_day": True}],
        approvals=12, mail={"reply_count": 21, "titles": [], "error": None},
        lab={"urgent": [], "today": [{"id": "1", "title": "t", "excerpt": "", "urgency": "today"}] * 3,
             "this_week": [{"id": "1", "title": "t", "excerpt": "", "urgency": "this_week"}], "fyi_count": 2})
    for kind in ("morning", "friday", "evening"):
        t = _build({**sec, "left_today": sec["today_must"], "done_today": sec["today_must"], "done_week": 15}, kind).text
        assert re.search(r"[0-9#*`>🔴]", t) is None, (kind, t)


def _battery():
    d = lambda **k: _sections(**k)  # noqa: E731
    items = [{"text": "회의 자료 정리", "date": "x"}, {"text": "학술대회 등록", "date": "x"}, {"text": "병원 예약", "date": "x"}]
    ev = [{"title": "가족 회의", "time": "10:00", "all_day": False, "start": "2026-10-06T01:00:00+00:00"},
          {"title": "정신과 상담", "time": "15:30", "all_day": False, "start": "2026-10-06T06:30:00+00:00"},
          {"title": "연속 측정", "time": "", "all_day": True, "start": "2026-10-06T00:00:00+00:00"}]
    lab = {"urgent": [{"id": "1", "title": "x", "excerpt": "", "urgency": "urgent"}] * 2,
           "today": [{"id": "2", "title": "y", "excerpt": "", "urgency": "today"}] * 5, "this_week": [], "fyi_count": 1}
    return [d(), d(today_must=items, rest_count=4, calendar=ev, approvals=7, lab=lab,
                   mail={"reply_count": 3, "titles": ["a"], "error": None},
                   supply={"items": [{"label": "s", "remaining": 1, "due": True, "hint": "h"}]}),
            d(calendar_note="캘린더 미연결", todo_error="할 일을 불러오지 못했습니다"),
            d(done_today=items, left_today=items, retro="오늘 막힌 일은 무엇이었나요?", approvals=1, lab=lab),
            d(done_week=9, today_must=items, calendar=ev, lab=lab)]


@pytest.mark.parametrize("kind", ["morning", "evening", "friday"])
@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("on", [False, True])
def test_full_output_passes_violations_for_fixture_battery(kind, short, on):
    for i, sec in enumerate(_battery()):
        for dd in (D, date(2026, 6, 1), date(2026, 12, 31)):
            o = script.ScriptOptions.default()
            o = script.ScriptOptions(**{**o.__dict__, "short": short, "say_supply": on, "say_todo_titles": on, "say_event_titles": on})
            r = script.build_script(kind, dd, sec, opts=o, friday=sec if kind == "morning" else None)
            assert coach_copy.violations(r.text) == [], (kind, i, r.text)
            assert not any(coach_copy.violations(x) for x in r.sentences)
            norm = unicodedata.normalize("NFKC", r.text)
            for term in voice_copy.SENSITIVE_TERMS:
                assert term not in norm.replace("개인 항목", "").lower() or term in ("회고",), (term, r.text)


def test_template_only_output_is_notify_safe_across_year():
    sec = _sections(today_must=[{"text": "회의 자료 정리", "date": "x"}], approvals=3,
                    calendar=[{"title": "가족 회의", "time": "10:00", "all_day": False, "start": "2026-10-06T01:00:00+00:00"}],
                    mail={"reply_count": 2, "titles": [], "error": None},
                    lab={"urgent": [{"id": "1", "title": "x", "excerpt": "", "urgency": "urgent"}], "today": [], "this_week": [],
                         "fyi_count": 0})
    for off in range(0, 366, 3):
        for kind in ("morning", "evening", "friday"):
            r = script.build_script(kind, D + timedelta(days=off), {**sec, "left_today": sec["today_must"]},
                                    opts=script.ScriptOptions(**{**script.ScriptOptions.default().__dict__,
                                                                 "say_todo_titles": False, "say_event_titles": False}))
            assert coach_copy.notify_safe(r.text), (kind, off, r.text)
            assert coach_copy.violations(r.text) == []


def test_verbalizer_outputs_are_coach_safe():
    from pa.voice import verbal
    outs = [verbal.count(n, u) for n in range(0, 100) for u in ("건",)]
    outs += [verbal.time_ko(f"{h:02d}:{m:02d}") for h in range(24) for m in range(0, 60, 5)]
    outs += [verbal.date_ko(D + timedelta(days=i)) for i in range(366)]
    for o in outs:
        assert coach_copy.violations(o) == [] and coach_copy.notify_safe(o), o


def test_script_module_is_pure():
    for mod in (script, voice_copy, __import__("pa.voice.verbal", fromlist=["x"])):
        src = open(mod.__file__, encoding="utf-8").read()
        for bad in ("sqlite3", "execute(", "utcnow", "datetime.now", "date.today", "time.time", "httpx", "requests",
                    "router", "open_store", "capacity", "preference", "open(", "os.environ"):
            assert bad not in src, (mod.__name__, bad)


_OK_TOP = {"__future__", "dataclasses", "datetime", "hashlib", "math", "re", "unicodedata", "typing", "zoneinfo", "pa"}
_ALLOWED_PA = ("pa", "pa.core", "pa.voice", "pa.core.coach_copy", "pa.core.briefing", "pa.voice.verbal", "pa.voice.voice_copy",
               "pa.voice.script")       # TODO(V1/V2): 자기 모듈(prefs·tts·cache·source)을 각자의 테스트와 함께 여기에 더한다


def _imported_names(src: str, pkg: list[str]) -> list[str]:
    """소스의 모든 import 를 절대 이름으로 푼다. 상대 import(`from ..core import capacity` → pa.core.capacity)도 포함."""
    import ast
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                mod = ".".join(pkg[:len(pkg) - (node.level - 1)] + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out += [f"{mod}.{a.name}" for a in node.names] + [mod]
    return out


PURE_MODULES = {"script.py", "verbal.py", "voice_copy.py"}      # strict: no I/O, no stdlib beyond the pure list
# I/O modules (V1): own explicit whitelists. No network/subprocess/socket, no private/counselor/briefing.
_IO_STDLIB = {
    "tts.py": {"__future__", "hashlib", "importlib", "io", "logging", "math", "sys", "wave", "typing"},
    "cache.py": {"__future__", "asyncio", "hashlib", "logging", "os", "re", "secrets", "datetime", "pathlib", "typing"},
    "prefs.py": {"__future__", "math", "sqlite3", "sys", "dataclasses"},
    "source.py": {"__future__", "collections", "datetime", "hashlib", "json", "sqlite3", "zoneinfo", "typing"},
}
_IO_PA = {
    "tts.py": set(),
    "cache.py": {"pa.voice.tts", "pa.voice.tts.TtsError", "pa.voice.tts.check_speed", "pa.voice.tts.engine_id",
                 "pa.voice.tts.synth_speed"},
    "prefs.py": {"pa.clock", "pa.clock.utcnow_iso", "pa.db", "pa.db.atomic"},
    "source.py": {"pa.core", "pa.core.capacity", "pa.voice", "pa.voice.script", "pa.voice.prefs", "pa.voice.script.build_script",
                  "pa.voice.script.pick_kind", "pa.voice.script.ScriptOptions", "pa.voice.script.VoiceScript",
                  "pa.voice.script.ALLOWED_SECTIONS", "pa.voice.prefs.VoiceSettings", "pa.rules", "pa.rules.Rules"},
}
_IO_FORBIDDEN = {"socket", "subprocess", "http", "urllib", "httpx", "requests", "aiohttp", "ssl", "ftplib", "smtplib"}


def _io_violations(name: str, names: list[str]) -> list[str]:
    bad = []
    for n in names:
        top = n.split(".")[0]
        if top == "pa":
            if n not in _IO_PA[name]:
                bad.append(n)
        elif top not in _IO_STDLIB[name] or top in _IO_FORBIDDEN:
            bad.append(n)
    return bad


def test_voice_io_modules_import_only_their_own_whitelist():
    import pathlib
    root = pathlib.Path(script.__file__).parent
    assert set(_IO_STDLIB) == {"tts.py", "cache.py", "prefs.py", "source.py"}
    # every voice module is classified: pure, io, or __init__ (a new module must pick a list)
    assert {f.name for f in root.rglob("*.py")} - PURE_MODULES - set(_IO_STDLIB) - {"__init__.py"} == set()
    for name in _IO_STDLIB:
        src = (root / name).read_text(encoding="utf-8")
        assert _io_violations(name, _imported_names(src, ["pa", "voice"])) == [], name


@pytest.mark.parametrize("src", ["import socket", "import subprocess", "import urllib.request", "from pa import private",
                                 "from pa.core import capacity", "import httpx", "from pa.core import briefing",
                                 "from pa.counselor import x"])
def test_io_whitelist_catches_new_imports(src):
    for name in _IO_STDLIB:
        if name == "source.py" and src == "from pa.core import capacity":
            continue        # source.py is the one voice module allowed to read the capacity level (V2)
        assert _io_violations(name, _imported_names(src, ["pa", "voice"])), (name, src)


def _not_whitelisted(names: list[str]) -> list[str]:
    return [n for n in names if n.split(".")[0] not in _OK_TOP or (n.split(".")[0] == "pa" and n not in _ALLOWED_PA)]


def test_voice_modules_import_only_whitelisted():
    """pa/voice/**/*.py 전부(하위 폴더 포함), 상대 import 포함."""
    import pathlib
    root = pathlib.Path(script.__file__).parent
    files = sorted(f for f in root.rglob("*.py") if f.name in PURE_MODULES or f.name == "__init__.py")
    assert {f.name for f in files} >= PURE_MODULES
    for f in files:
        pkg = ["pa", *f.parent.relative_to(root.parent).parts]
        assert _not_whitelisted(_imported_names(f.read_text(encoding="utf-8"), pkg)) == [], f.name


@pytest.mark.parametrize("src,bad", [
    ("from ..core import capacity", "pa.core.capacity"), ("from .. import private", "pa.private"),
    ("from ..core.capacity import get_signal", "pa.core.capacity"), ("from . import source", "pa.voice.source"),
    ("import sqlite3", "sqlite3"), ("from pa.core import capacity", "pa.core.capacity"),
])
def test_import_whitelist_catches_relative_and_absolute_escapes(src, bad):
    assert bad in _not_whitelisted(_imported_names(src, ["pa", "voice"]))


def test_import_whitelist_accepts_the_real_style():
    assert _not_whitelisted(_imported_names("from . import verbal\nfrom . import voice_copy as vc\nfrom pa.core import coach_copy",
                                            ["pa", "voice"])) == []


def test_build_script_does_not_mutate_input():
    import copy
    sec = _sections(today_must=[{"text": "가 나", "date": "x"}], calendar=[{"title": "t", "time": "10:00", "all_day": False}])
    before = copy.deepcopy({**sec, **PRIVATE_FIXTURE})
    _build({**sec, **PRIVATE_FIXTURE})
    assert {**sec, **PRIVATE_FIXTURE} == before


def test_no_llm_call(monkeypatch):
    import pa.llm.router as router
    called = []
    monkeypatch.setattr(router, "run", lambda *a, **k: called.append(1), raising=False)
    _build(_sections(today_must=[{"text": "a", "date": "x"}]))
    assert called == []

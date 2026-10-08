"""V0(ScriptOptions)와 V1(VoiceSettings)의 낭독 스위치 기본값이 항상 같아야 한다.

개인정보 때문에 일정·할 일 제목은 기본으로 읽지 않는다(Stage 7 계획 D4/D5).
"""
import dataclasses

from pa.voice import prefs, script

SWITCHES = ("say_todo_titles", "say_event_titles", "say_mail_subjects", "say_lab_titles", "say_supply")


def _defaults(cls):
    return {f.name: f.default for f in dataclasses.fields(cls) if f.name in SWITCHES}


def test_switch_defaults_match_between_prefs_and_script():
    assert _defaults(prefs.VoiceSettings) == _defaults(script.ScriptOptions)


def test_titles_are_off_by_default():
    d = _defaults(prefs.VoiceSettings)
    assert d["say_todo_titles"] is False and d["say_event_titles"] is False
    assert all(v is False for v in d.values())

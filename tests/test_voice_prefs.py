import sqlite3
import sys

import pytest

from pa.voice import prefs
from pa.voice.prefs import VoiceSettings, get_settings, set_settings


def test_defaults_when_empty(con, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    s = get_settings(con)
    assert s == VoiceSettings(engine="none")
    assert s.enabled and s.rate == 1.0
    # privacy first: titles are NOT read aloud by default
    assert not (s.say_todo_titles or s.say_event_titles)
    assert not s.say_supply
    monkeypatch.setattr(sys, "platform", "win32")
    assert get_settings(con).engine == "windows"


def test_stored_explicit_on_turns_titles_on(con):
    con.execute("INSERT INTO preference(key,value,updated_at) VALUES('voice.say_todo_titles','1','x'),"
                "('voice.say_event_titles','1','x')")
    s = get_settings(con)
    assert s.say_todo_titles and s.say_event_titles


def test_roundtrip_set_get(con):
    s = set_settings(con, rate=1.2, engine="none", say_supply=True, enabled=False)
    assert get_settings(con) == s
    assert s.rate == 1.2 and s.say_supply and not s.enabled
    # partial update keeps the rest
    s2 = set_settings(con, rate=0.8)
    assert s2.say_supply and s2.rate == 0.8


@pytest.mark.parametrize("changes", [
    {"nope": 1},
    {"rate": 2.0}, {"rate": 0}, {"rate": -1.0}, {"rate": "1.0"}, {"rate": True}, {"rate": float("nan")},
    {"engine": "edge"}, {"engine": ""}, {"engine": None}, {"engine": "WINDOWS"},
    {"enabled": "yes"}, {"enabled": 1}, {"enabled": None}, {"say_supply": "true"},
    {"max_chars": 300}, {"max_sentences": 12},      # D9: code constants in the script builder, not settings
])
def test_rejects_invalid(con, changes):
    before = get_settings(con)
    with pytest.raises(ValueError):
        set_settings(con, **changes)
    assert get_settings(con) == before


def test_invalid_change_writes_nothing_atomic(con):
    with pytest.raises(ValueError):
        set_settings(con, rate=1.5, engine="edge")
    assert con.execute("SELECT COUNT(*) FROM preference WHERE key LIKE 'voice.%'").fetchone()[0] == 0


def test_no_cloud_engine_in_engines():
    assert prefs.ENGINES == ("windows", "none")
    for e in prefs.ENGINES:
        for bad in ("edge", "google", "azure", "polly", "openai", "cloud", "gtts", "eleven", "http"):
            assert bad not in e.lower()


@pytest.mark.parametrize("cloud", ["edge", "edge-tts", "google", "azure", "polly", "openai", "cloud",
                                   "cloud voice service", "gtts", "elevenlabs", "https://tts.example/x"])
def test_no_value_can_enable_cloud_engine(con, cloud):
    with pytest.raises(ValueError):
        set_settings(con, engine=cloud)
    # even a hand-written DB row cannot select it
    con.execute("INSERT INTO preference(key,value,updated_at) VALUES('voice.engine',?,'x')", (cloud,))
    assert get_settings(con).engine in prefs.ENGINES
    from pa.voice.tts import make_engine
    eng = make_engine(cloud)
    assert eng.name in ("none", "unavailable") and not eng.available()


def test_corrupt_value_falls_back_to_default(con, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    for k, v in [("rate", "abc"), ("engine", "edge"), ("enabled", "maybe"), ("say_supply", "2")]:
        con.execute("INSERT INTO preference(key,value,updated_at) VALUES(?,?,'x')", (prefs.KEYS[k], v))
    assert get_settings(con) == VoiceSettings(engine="none")


def test_rate_out_of_set_in_db_falls_back(con):
    con.execute("INSERT INTO preference(key,value,updated_at) VALUES('voice.rate','9',?)", ("x",))
    assert get_settings(con).rate == 1.0


def test_only_voice_namespace_nonprivate_values_stored(con):
    set_settings(con, rate=1.5)
    rows = con.execute("SELECT key, value FROM preference").fetchall()
    assert rows
    for k, v in rows:
        assert k in prefs.KEYS.values()
        assert v in ("0", "1") or v.replace(".", "").isdigit() or v in prefs.ENGINES
    assert set(prefs.KEYS) == {f.name for f in __import__("dataclasses").fields(VoiceSettings)}
    # settings never touch the private database module
    assert "private" not in open(prefs.__file__, encoding="utf-8").read().lower().replace("non-private", "")


def test_updated_at_is_set(con):
    set_settings(con, rate=1.2)
    assert con.execute("SELECT updated_at FROM preference WHERE key='voice.rate'").fetchone()[0]


def test_set_settings_is_atomic_on_mid_loop_db_error(con):
    set_settings(con, enabled=True, rate=1.0)
    con.execute("CREATE TRIGGER voice_rate_boom BEFORE INSERT ON preference WHEN NEW.key='voice.rate' "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    con.execute("CREATE TRIGGER voice_rate_boom2 BEFORE UPDATE ON preference WHEN NEW.key='voice.rate' "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    before = dict(con.execute("SELECT key, value FROM preference").fetchall())
    with pytest.raises(sqlite3.DatabaseError):
        set_settings(con, enabled=False, say_supply=True, rate=1.2)
    assert dict(con.execute("SELECT key, value FROM preference").fetchall()) == before
    assert not con.in_transaction


def test_set_settings_nests_inside_an_outer_transaction(con):
    from pa.db import atomic
    with atomic(con):
        set_settings(con, rate=1.5)
    assert get_settings(con).rate == 1.5
    with pytest.raises(RuntimeError):
        with atomic(con):
            set_settings(con, rate=0.8)
            raise RuntimeError
    assert get_settings(con).rate == 1.5


def test_settings_have_no_length_caps():
    from dataclasses import fields
    assert not {"max_chars", "max_sentences"} & {f.name for f in fields(VoiceSettings)}
    assert not {"max_chars", "max_sentences"} & set(prefs.KEYS)

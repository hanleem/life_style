"""Voice settings in the core `preference` table (keys `voice.*`). Non-private: only toggles,
an engine name and a playback rate. Strictly validated; unknown keys are rejected.

`rate` is a CLIENT-SIDE playback rate (audio.playbackRate); the engine always synthesizes at normal
speed and the audio cache key does not depend on it. Script length caps are code constants in the
script builder (plan D9), not settings."""
from __future__ import annotations

import math
import sqlite3
import sys
from dataclasses import dataclass, fields, replace

from pa.clock import utcnow_iso
from pa.db import atomic

KEYS = {
    "enabled": "voice.enabled",
    "engine": "voice.engine",
    "rate": "voice.rate",
    "say_todo_titles": "voice.say_todo_titles",
    "say_event_titles": "voice.say_event_titles",
    "say_supply": "voice.say_supply",
}
RATES = (0.8, 1.0, 1.2, 1.5)
ENGINES = ("windows", "none")          # no cloud engine, by design (plan D7)
_BOOLS = ("enabled", "say_todo_titles", "say_event_titles", "say_supply")


def _default_engine() -> str:
    return "windows" if sys.platform == "win32" else "none"


@dataclass(frozen=True)
class VoiceSettings:
    enabled: bool = True
    engine: str = "none"
    rate: float = 1.0
    say_todo_titles: bool = False
    say_event_titles: bool = False
    say_supply: bool = False


def _defaults() -> VoiceSettings:
    return VoiceSettings(engine=_default_engine())


def _validate(name: str, v):
    if name in _BOOLS:
        if not isinstance(v, bool):
            raise ValueError(f"{name}: 켜짐/꺼짐(참/거짓) 값만 허용됩니다")
        return v
    if name == "engine":
        if not isinstance(v, str) or v not in ENGINES:
            raise ValueError(f"engine: {', '.join(ENGINES)} 중 하나여야 합니다")
        return v
    if name == "rate":
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or float(v) not in RATES:
            raise ValueError(f"rate: {', '.join(str(r) for r in RATES)} 중 하나여야 합니다")
        return float(v)
    raise ValueError(f"알 수 없는 설정입니다: {name!r}")


def _parse(name: str, raw: str):
    """Stored text -> value, or raise ValueError (caller falls back to the default)."""
    if name in _BOOLS:
        if raw not in ("0", "1"):
            raise ValueError
        return raw == "1"
    if name == "rate":
        return _validate(name, float(raw))
    return _validate(name, raw)


def _dump(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def get_settings(con: sqlite3.Connection) -> VoiceSettings:
    s = _defaults()
    rows = dict(con.execute("SELECT key, value FROM preference WHERE key LIKE 'voice.%'").fetchall())
    for name, key in KEYS.items():
        raw = rows.get(key)
        if raw is None:
            continue
        try:
            s = replace(s, **{name: _parse(name, raw)})
        except (ValueError, TypeError):
            pass
    return s


def set_settings(con: sqlite3.Connection, **changes) -> VoiceSettings:
    valid_names = {f.name for f in fields(VoiceSettings)}
    clean = {}
    for name, v in changes.items():
        if name not in valid_names:
            raise ValueError(f"알 수 없는 설정입니다: {name!r}")
        clean[name] = _validate(name, v)
    now = utcnow_iso()
    with atomic(con):   # SAVEPOINT: all-or-nothing even on an autocommit connection, nests safely
        for name, v in clean.items():
            con.execute(
                "INSERT INTO preference(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (KEYS[name], _dump(v), now))
    return get_settings(con)

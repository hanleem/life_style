"""Loads TODAY's stored briefing sections and builds the spoken script. The only voice module that reads briefing
data. Reads the `briefing` table (kind and sections_json columns only). Voice settings come from prefs
(preference table) via the caller."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

from pa.voice import script
from pa.voice.prefs import VoiceSettings

KST = ZoneInfo("Asia/Seoul")
KINDS = ("morning", "evening", "friday")
_LRU: OrderedDict = OrderedDict()
_LRU_SIZE = 8


class NoBriefing(Exception):
    """Reserved for callers that prefer an exception over None."""


def _parse(raw) -> dict | None:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if k in script.ALLOWED_SECTIONS}      # allowlist before anything else sees it


def _digest(sections: dict | None) -> str:
    return hashlib.sha256(json.dumps(sections, sort_keys=True, default=str).encode()).hexdigest()[:16]


def load_today(con, now: datetime, rules, settings: VoiceSettings, *, kind: str | None = None) -> script.VoiceScript | None:
    if not settings.enabled:
        return None
    local = now.astimezone(KST)
    today = local.date()
    rows = con.execute("SELECT kind, sections_json FROM briefing WHERE for_date=?", (today.isoformat(),)).fetchall()
    raw = {r["kind"]: r["sections_json"] for r in rows if r["kind"] in KINDS}
    if kind is None:
        kind = script.pick_kind(set(raw), local, rules.brief_evening_on(today.weekday()))
    elif kind not in raw:
        return None          # 고른 브리핑이 오늘 없으면 다른 것으로 바꿔 읽지 않는다
    if kind is None:
        return None
    sections = _parse(raw[kind])
    if sections is None:
        return None
    friday = _parse(raw["friday"]) if kind == "morning" and "friday" in raw else None
    short = False         # TODO: 짧은 낭독 모드는 사용자가 고르는 설정으로 바꾼다
    must_cap = 3
    key = (kind, today.isoformat(), _digest(sections), _digest(friday), short, must_cap, settings)
    hit = _LRU.get(key)
    if hit is not None:
        _LRU.move_to_end(key)
        return hit
    opts = script.ScriptOptions(short=short, must_cap=must_cap, say_todo_titles=settings.say_todo_titles,
                                say_event_titles=settings.say_event_titles, say_supply=settings.say_supply)
    out = script.build_script(kind, today, sections, opts=opts, friday=friday)
    _LRU[key] = out
    while len(_LRU) > _LRU_SIZE:
        _LRU.popitem(last=False)
    return out

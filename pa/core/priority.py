"""우선순위 엔진 rank() — 순수 함수(I/O·시계·DB 없음, 현재 시각은 인자로 주입, 판단은 KST).

단계(앞선 것이 먼저): safety -> due_today -> overdue -> waiting -> appointment -> this_week -> next_week -> rest.
우선순위는 구조화된 사실(마감·기다리는 사람·일정 시각·알림의 규칙 기반 urgency/safety)로만 정한다.
항목의 표시 문구는 정렬·판정에 쓰지 않는다(P-prio-1) — 읽는 곳은 `_validate`(형식 검사)와 `_present`(결과 dict)뿐.

정렬 규칙(단계 안, 모두 오름차순): 아래 단계별 키 -> 대표 출처 SOURCE_RANK -> 대표 key 문자열. 입력 순서와 무관하다.
중복 제거: key 또는 links 문자열이 정확히 같은 항목만 union-find로 합친다(유사 문구 병합 없음).

rest_count 의미(기존 '할 일 중 cap 초과분'에서 변경): top 밖에 남은 항목 중 단계가 `rest`가 아닌 것의 수.
브리핑 "(그 외 n건)"은 이 숫자이며, 막연한 백로그(rest 단계)는 세지 않는다.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pa.core import briefing_copy as C

KST = ZoneInfo("Asia/Seoul")

TIERS = ("safety", "due_today", "overdue", "waiting", "appointment", "this_week", "next_week", "rest")
_TIER_IDX = {t: i for i, t in enumerate(TIERS)}
REASONS = {
    "safety": C.REASON_SAFETY, "due_today": C.REASON_DUE_TODAY, "overdue": C.REASON_OVERDUE,
    "waiting": C.REASON_WAITING, "appointment": C.REASON_APPOINTMENT,
    "this_week": C.REASON_THIS_WEEK, "next_week": C.REASON_NEXT_WEEK,
}
SOURCE_RANK = {"task": 0, "calendar": 2, "approvals": 6}
MODES = ("morning", "evening", "friday", "rest_day")
# 모드별 보여 줄 단계. morning은 rest(백로그)까지, 나머지는 명시한 단계만. next_week는 friday에서만 생긴다.
# evening에 safety를 넣은 것은 계획 문구('due_today, overdue, waiting만')에서 의도적으로 벗어난 점: 안전 알림은 어느 모드에서도 숨기지 않는다.
_MODE_TIERS = {
    "morning": frozenset(TIERS) - {"next_week"},
    "evening": frozenset({"safety", "due_today", "overdue", "waiting", "appointment"}),
    "friday": frozenset({"safety", "due_today", "overdue", "waiting", "this_week", "next_week"}),
    "rest_day": frozenset({"safety", "due_today", "overdue"}),
}
_URGENCY = {"urgent": 3, "today": 2, "this_week": 1, "fyi": 0}
_MAX_REASONS = 2


@dataclass(frozen=True)
class Item:
    key: str
    source: str
    text: str
    due_at: datetime | None = None
    due_all_day: bool = False
    start_at: datetime | None = None
    end_at: datetime | None = None
    waiting_since: datetime | None = None
    alert_urgency: str | None = None
    alert_safety: bool = False
    links: frozenset[str] = frozenset()
    task_id: str | None = None
    auto: bool = False
    meeting: bool = False
    created_at: datetime | None = None
    note: str | None = None   # 고정 이유 문구(결정적 규칙이 만든 항목용). 정렬·판정에는 쓰지 않는다


@dataclass(frozen=True)
class Ranked:
    top: list[dict]
    rest_count: int
    all: list[dict]
    excluded: tuple[str, ...] = ()   # 모드/종료로 목록에서 빠진 항목의 대표 key(정렬됨). ranked ∪ excluded = 입력(병합 후)


def _aware(dt, name: str, i: int, *, allow_none=True):
    if dt is None and allow_none:
        return None
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"items[{i}].{name} must be a timezone-aware datetime")
    return dt


def _validate(raw, i: int) -> Item:
    """형식 검사. 메시지에는 필드 이름과 위치만 담는다(내용 없음)."""
    if isinstance(raw, Mapping):
        names = {f.name for f in fields(Item)}
        extra = set(raw) - names
        missing = {"key", "source", "text"} - set(raw)
        if extra or missing:
            raise ValueError(f"items[{i}] bad keys: missing={sorted(missing)} unknown={sorted(extra)}")
        raw = Item(**raw)
    if not isinstance(raw, Item):
        raise ValueError(f"items[{i}] must be Item")
    if not isinstance(raw.key, str) or not raw.key.strip():
        raise ValueError(f"items[{i}].key must be a non-empty str")
    if raw.source not in SOURCE_RANK:
        raise ValueError(f"items[{i}].source unknown")
    if not isinstance(raw.text, str):
        raise ValueError(f"items[{i}].text must be str")
    for n in ("due_at", "start_at", "end_at", "waiting_since", "created_at"):
        _aware(getattr(raw, n), n, i)
    for n in ("due_all_day", "alert_safety", "auto", "meeting"):
        if not isinstance(getattr(raw, n), bool):
            raise ValueError(f"items[{i}].{n} must be bool")
    if raw.alert_urgency is not None and raw.alert_urgency not in _URGENCY:
        raise ValueError(f"items[{i}].alert_urgency unknown")
    if raw.note is not None and not isinstance(raw.note, str):
        raise ValueError(f"items[{i}].note must be str or None")
    if raw.task_id is not None and not isinstance(raw.task_id, str):
        raise ValueError(f"items[{i}].task_id must be str or None")
    if isinstance(raw.links, (str, bytes)) or not isinstance(raw.links, Iterable):
        raise ValueError(f"items[{i}].links must be a set of str")
    links = frozenset(raw.links)
    if any(not isinstance(l, str) or not l.strip() for l in links):
        raise ValueError(f"items[{i}].links must contain non-empty str")
    return raw if isinstance(raw.links, frozenset) else Item(**{**{f.name: getattr(raw, f.name) for f in fields(Item)}, "links": links})


def _kdate(dt: datetime) -> date:
    return dt.astimezone(KST).date()


def _ts(dt: datetime | None):
    """None은 항상 뒤로: (1, 0) > (0, 어떤 값)."""
    return (1, 0.0) if dt is None else (0, dt.timestamp())


@dataclass
class _Facts:
    """병합된 한 항목의 판정 근거(출처 항목들의 합집합)."""
    members: list[Item]
    due_at: datetime | None
    due_all_day: bool
    start_at: datetime | None
    end_at: datetime | None
    waiting_since: datetime | None
    created_at: datetime | None
    rep: Item


def _member_tiers(m: Item, now: datetime, today: date, mode: str) -> dict[str, str]:
    """한 출처 항목이 해당하는 단계 -> 이유 문구. 비어 있으면 '아무 단계도 아님'(종료된 일정)."""
    out: dict[str, str] = {}
    due_d = _kdate(m.due_at) if m.due_at is not None else None
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    if m.alert_urgency == "urgent" and m.alert_safety:
        out["safety"] = REASONS["safety"]
    if due_d == today or m.alert_urgency == "today":
        out["due_today"] = REASONS["due_today"]
    if due_d is not None and due_d < today:
        out["overdue"] = REASONS["overdue"]
    if m.waiting_since is not None or m.alert_urgency == "urgent":
        out["waiting"] = m.note if (m.note and m.waiting_since is not None) else REASONS["waiting"]
    if m.start_at is not None and _kdate(m.start_at) == today and (m.end_at or m.start_at) > now:
        out["appointment"] = REASONS["appointment"].format(hhmm=m.start_at.astimezone(KST).strftime("%H:%M"))
    if not ({"due_today", "overdue"} & out.keys()):   # 오늘 마감·기한 지남이면 주간 이유는 덧붙이지 않는다
        if (due_d is not None and today < due_d <= sunday) or m.alert_urgency == "this_week":
            out["this_week"] = REASONS["this_week"]
        elif mode == "friday" and due_d is not None and monday + timedelta(days=7) <= due_d <= sunday + timedelta(days=7):
            out["next_week"] = REASONS["next_week"]
    if not out:
        ended = (m.start_at is not None or m.end_at is not None) and \
            ((m.end_at or m.start_at) <= now)
        if not ended:
            out["rest"] = ""
    return out


def _groups(items: list[Item]) -> list[list[Item]]:
    """key·links 문자열이 정확히 같은 항목끼리 union-find."""
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen: dict[str, int] = {}
    for i, it in enumerate(items):
        for tok in (it.key, *it.links):
            j = seen.setdefault(tok, i)
            if j != i:
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
    out: dict[int, list[Item]] = {}
    for i, it in enumerate(items):
        out.setdefault(find(i), []).append(it)
    return list(out.values())


def _min_dt(vals):
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def _facts(members: list[Item]) -> _Facts:
    members = sorted(members, key=lambda m: (SOURCE_RANK[m.source], m.key, m.text))   # text는 완전 동률일 때 표시 문구만 고정
    dues = [m for m in members if m.due_at is not None]
    best = min(dues, key=lambda m: (_kdate(m.due_at), m.due_all_day, m.due_at.timestamp()), default=None)
    return _Facts(members, best.due_at if best else None, best.due_all_day if best else False,
                  _min_dt(m.start_at for m in members), _min_dt(m.end_at for m in members),
                  _min_dt(m.waiting_since for m in members), _min_dt(m.created_at for m in members), members[0])


def _sort_key(tier: str, f: _Facts):
    if tier == "due_today":
        grp = 2 if f.due_at is None else (1 if f.due_all_day else 0)   # 시각 있는 것 -> 종일 -> 날짜 없는 알림
        k = (grp, _ts(f.due_at))
    elif tier == "overdue":
        k = (_ts(f.due_at),)
    elif tier == "waiting":
        k = (_ts(f.waiting_since), _ts(f.created_at))
    elif tier == "appointment":
        k = (_ts(f.start_at),)
    elif tier in ("this_week", "next_week"):
        k = (_ts(f.due_at),)
    elif tier == "rest":
        k = (_ts(f.due_at), _ts(f.created_at))
    else:   # safety
        k = (_ts(f.created_at),)
    return (*k, SOURCE_RANK[f.rep.source], f.rep.key)


def _present(f: _Facts, tier: str, reasons: list[str]) -> dict:
    ms = f.members
    tids = sorted(m.task_id for m in ms if m.task_id is not None)
    return {
        "id": f.rep.key, "text": f.rep.text,
        "date": _kdate(f.due_at).isoformat() if f.due_at is not None else None,
        "source": f.rep.source, "reasons": reasons, "tier": tier,
        "task_id": tids[0] if tids else None,
        "auto": any(m.auto for m in ms), "meeting": any(m.meeting for m in ms),
        "keys": sorted({m.key for m in ms}),
    }


def rank(items, now: datetime, *, cap: int, mode: Literal["morning", "evening", "friday", "rest_day"]) -> Ranked:
    """항목들을 우선순위 순으로 병합·정렬한다. 입력 순서와 무관한 결정적 결과.

    잘못된 입력(naive 시각, 문자열이 아닌 text, 알 수 없는 출처 등)은 ValueError.
    now가 naive이거나 cap이 음수/비정수이거나 mode가 목록에 없어도 ValueError.
    ValueError 메시지에는 항목 내용을 담지 않는다.
    """
    if mode not in _MODE_TIERS:
        raise ValueError("unknown mode")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise ValueError("cap must be a non-negative int")
    _aware(now, "now", -1, allow_none=False)
    valid = [_validate(r, i) for i, r in enumerate(items)]
    today = _kdate(now)
    allowed = _MODE_TIERS[mode]
    ranked: list[tuple[tuple, dict]] = []
    excluded: list[str] = []
    for members in _groups(valid):
        f = _facts(members)
        merged: dict[str, str] = {}
        for m in members:
            for t, r in _member_tiers(m, now, today, mode).items():
                merged.setdefault(t, r)
        tier = min(merged, key=_TIER_IDX.__getitem__) if merged else None
        if tier is None or tier not in allowed:
            excluded.append(f.rep.key)
            continue
        reasons = [merged[t] for t in sorted((t for t in merged if t != "rest"), key=_TIER_IDX.__getitem__)][:_MAX_REASONS]
        ranked.append(((_TIER_IDX[tier], *_sort_key(tier, f)), _present(f, tier, reasons)))
    ranked.sort(key=lambda p: p[0])
    out = [d for _, d in ranked]
    top = out[:cap]
    return Ranked(top=top, rest_count=sum(1 for d in out[cap:] if d["tier"] != "rest"), all=out,
                  excluded=tuple(sorted(excluded)))

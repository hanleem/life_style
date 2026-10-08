"""브리핑 섹션 → 낭독 문장 목록. 순수 함수: DB·시계·네트워크 접근 없음(시각은 인자로만 받는다).

LLM 을 쓰지 않는 이유(리뷰 확인용): 결정적 출력이라 해시로 캐시할 수 있고, 금칙어 테스트로 완전히 검증할 수 있고,
지연이 0 이며, 문장이 어디로도 나가지 않는다. 로컬 LLM 다듬기는 이 단계에서 하지 않는다.

허용목록: 입력 dict 는 ALLOWED_SECTIONS 키로 먼저 거른 사본만 읽는다. 건강·일기 같은
개인 기록과 여력 신호는 이 모듈에 들어올 길이 없다(여력이 낮은 날의 짧은 모드는 호출부가 `short` 로만 알려 준다).
"""
import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from pa.core import coach_copy
from pa.voice import verbal, voice_copy as vc

SCRIPT_VERSION = "v1"
MAX_CHARS = 300
MAX_SENTENCES = 12
MAX_CHARS_SHORT = 180
MAX_SENTENCES_SHORT = 7
CHARS_PER_SEC = 5.0
TITLE_MAX = 30
TITLE_MIN = 15          # 예산이 모자랄 때 제목을 줄이는 길이
CAL_ITEMS_MAX = 3
KST = ZoneInfo("Asia/Seoul")

ALLOWED_SECTIONS = frozenset({"today_must", "rest_count", "calendar", "calendar_note", "todo_error", "approvals",
                              "supply", "done_today", "left_today", "done_week", "retro"})


@dataclass(frozen=True)
class ScriptOptions:
    short: bool = False
    must_cap: int = 3
    # 기본은 끔(D4·D5, 소유자 결정): 제목은 사용자가 입력한 문장이라 민감어 목록으로 완전히 거를 수 없다.
    # 끄면 개수·시각·고정 문장만 읽고 제목 글자는 한 글자도 나가지 않는다.
    say_todo_titles: bool = False
    say_event_titles: bool = False
    say_supply: bool = False

    @classmethod
    def default(cls) -> "ScriptOptions":
        return cls()


@dataclass(frozen=True)
class Sentence:
    text: str
    prio: int
    tag: str = ""        # "item" = 다시 줄일 수 있는 할 일 문장, "rest" = 다시 계산되는 '그 밖에 n건'
    title: str = ""      # tag == "item" 일 때 가림·낭독 처리가 끝난 제목(자르기 전)
    prefix: str = ""


@dataclass(frozen=True)
class VoiceScript:
    kind: str
    for_date: date
    sentences: tuple[str, ...]
    text: str
    script_hash: str
    est_seconds: int
    dropped: int


def _allowed(sections) -> dict:
    if not isinstance(sections, dict):
        return {}
    return {k: v for k, v in (sections or {}).items() if k in ALLOWED_SECTIONS}


# --- 제목 정리 ---------------------------------------------------------------------------------
_URL = re.compile(r"https?://\S+|www\.\S+|\S*@\S+", re.I)      # 주소·이메일·@멘션은 읽지 않는다
_HASHREF = re.compile(r"\(\s*#\d+\s*\)|#\d+")
_MD = re.compile(r"[#*_>`]")
_TILDE_RANGE = re.compile(r"(?<=\d)\s*~\s*(?=\d)")             # 10~12시 → 낭독기가 '열 시에서 열두 시'로 읽는다


def _clean(text) -> str:
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = "".join(" " if c in "\t\n\r\x0b\x0c" else c for c in t)
    t = "".join(c for c in t if unicodedata.category(c) not in ("Cf", "Cc", "So", "Sk"))
    t = _URL.sub(" ", t)
    t = _TILDE_RANGE.sub("\x00", t).replace("~", " ").replace("\x00", "~")   # 숫자 범위의 ~ 만 낭독기로 넘긴다
    t = _MD.sub("", _HASHREF.sub(" ", t))
    return " ".join(t.split())


def _truncate(t: str, n: int) -> str:
    if len(t) <= n:
        return t
    cut = t[:n]
    if t[n] != " " and " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip()


def clean_title(text) -> str:
    """머리표·주소·마크다운·이모지·해시 참조를 지우고 TITLE_MAX 자로 자른다(말줄임표 없음)."""
    return _truncate(_clean(text), TITLE_MAX)


def _spoken(text) -> str | None:
    """낭독용 제목(자르기 전). 문자열이 아니거나, 민감하거나, 금칙 표현이 있거나, 비면 None(= 가림). 숫자·딕셔너리를 str() 로
    바꿔 읽는 일은 없다."""
    if not isinstance(text, str):
        return None
    if vc.is_sensitive(text):
        return None
    cleaned = _clean(text)
    if not cleaned or vc.is_sensitive(cleaned) or coach_copy.violations(cleaned):
        return None
    spoken = verbal.speak_digits(cleaned)
    return None if vc.is_sensitive(spoken) or coach_copy.violations(spoken) else spoken


def _fit(title: str, n: int) -> str:
    """자른 결과가 새로 민감해지는 경우(예: '잠정'이 '잠'으로 잘림)도 가린다."""
    t = _truncate(title, n)
    return vc.PRIVATE_ITEM if not t or vc.is_sensitive(t) or coach_copy.violations(t) else t


def _title(text) -> str:
    """제목처럼 자리를 비울 수 없는 곳: 가려지면 '개인 항목'."""
    s = _spoken(text)
    return vc.PRIVATE_ITEM if s is None else _fit(s, TITLE_MAX)


def _readable(text) -> str | None:
    """제목이 어느 길이로 잘려도 안전하면 낭독용 제목, 아니면 None (가려진 항목은 개수 문장으로만 센다)."""
    sp = _spoken(text)
    if sp is None:
        return None
    return None if vc.PRIVATE_ITEM in (_fit(sp, TITLE_MAX), _fit(sp, TITLE_MIN)) else sp


# --- 문장 조립 -----------------------------------------------------------------------------------
def _n(v) -> int:
    try:
        return max(int(v), 0)
    except (TypeError, ValueError):
        return 0


def _lst(v) -> list:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, (list, tuple)) else []


def _event_parts(e: dict, weekday: bool) -> tuple[str, str]:
    """(요일, 시각): '월요일'(다음 주 일정만), '오전 아홉 시'. 없으면 ''."""
    when = verbal.time_ko(e.get("time"))
    wd = ""
    if weekday:
        try:
            wd = verbal.weekday_ko(datetime.fromisoformat(str(e.get("start"))).astimezone(KST).date())
        except (ValueError, TypeError):
            wd = ""
    return wd, when


def _event_label(e: dict, weekday: bool) -> str:
    """'오전 아홉 시' (다음 주 일정은 앞에 '월요일'). 시각이 없으면 ''."""
    wd, when = _event_parts(e, weekday)
    if weekday:
        return " ".join(x for x in (wd, when if when else ("종일" if e.get("all_day") else "")) if x)
    return when


def _calendar_titles_off(out: list, events: list, friday: bool) -> None:
    """제목 낭독이 꺼진(기본) 일정: 시각만 고정 문장으로. 제목 글자는 쓰지 않는다."""
    for e in events[:CAL_ITEMS_MAX]:
        wd, when = _event_parts(e, friday)
        if when:
            out.append(Sentence(vc.CAL_TIME.format(when=" ".join(x for x in (wd, when) if x)), 6))
        elif e.get("all_day"):
            out.append(Sentence(vc.CAL_ALLDAY_ONLY.format(when=wd) if wd else vc.CAL_ALLDAY_PLAIN, 6))
    if len(events) > CAL_ITEMS_MAX:
        out.append(Sentence(vc.CAL_MORE.format(n=verbal.count(len(events) - CAL_ITEMS_MAX)), 6))


def _calendar(out: list, s: dict, kind: str, opts: ScriptOptions, head_prio: int = 3) -> None:
    friday = kind == "friday"
    if s.get("calendar_note"):
        out.append(Sentence(vc.CAL_ERR, head_prio))
        return
    events = _lst(s.get("calendar"))
    head = vc.CAL_HEAD["friday" if friday else "morning"]
    if not events:
        out.append(Sentence(vc.NEXT_WEEK_NONE if friday else vc.CAL_NONE, head_prio))
        return
    out.append(Sentence(head.format(n=verbal.count(len(events))), head_prio))
    if opts.short:
        return
    if not opts.say_event_titles:
        _calendar_titles_off(out, events, friday)
        return
    visible, masked = [], 0
    for e in events:
        title = _readable(e.get("title"))
        if title is None:
            masked += 1          # 가려진 일정은 시각도 말하지 않는다(개수만)
        else:
            visible.append((e, title))
    for e, title in visible[:CAL_ITEMS_MAX]:
        title = _fit(title, TITLE_MAX)
        label = _event_label(e, friday)
        if label:
            line = vc.CAL_ITEM.format(time=label, title=title)
        elif e.get("all_day"):
            line = vc.CAL_ALLDAY.format(title=title)
        else:
            line = vc.CAL_TITLE_ONLY.format(title=title)
        out.append(Sentence(line, 6))
    if len(visible) > CAL_ITEMS_MAX:
        out.append(Sentence(vc.CAL_MORE.format(n=verbal.count(len(visible) - CAL_ITEMS_MAX)), 6))
    if masked:
        out.append(Sentence(vc.PRIVATE_EVENTS.format(n=verbal.count(masked)), 6))


def _items(out: list, titles: list, opts: ScriptOptions, rest_base: int = 0) -> None:
    """할 일 문장(P2)과 '그 밖에 n건'(P3). 제목 낭독이 꺼져 있으면(기본) 제목은 한 글자도 쓰지 않는다."""
    if not opts.say_todo_titles:
        if rest_base > 0:
            out.append(Sentence(vc.REST.format(n=verbal.count(rest_base)), 3, "rest", prefix=str(rest_base)))
        return
    visible, masked = [], 0
    for text in titles:
        sp = _readable(text)
        if sp is None:
            masked += 1
        else:
            visible.append(sp)
    shown = visible[:max(opts.must_cap, 0)]
    for i, title in enumerate(shown):
        prefix = vc.ITEM_PREFIXES[min(i, len(vc.ITEM_PREFIXES) - 1)]
        out.append(Sentence("", 2, "item", title, prefix))
    if masked:
        out.append(Sentence(vc.PRIVATE_TODOS.format(n=verbal.count(masked)), 3, "private"))
    rest = rest_base + len(visible) - len(shown)
    if rest > 0:
        out.append(Sentence(vc.REST.format(n=verbal.count(rest)), 3, "rest", prefix=str(rest)))


def _render_items(sents: list[Sentence], n: int) -> list[Sentence]:
    out = []
    for s in sents:
        if s.tag == "item":
            s = Sentence(vc.ITEM.format(prefix=s.prefix, title=_fit(s.title, n)), 2, "item", s.title, s.prefix)
        out.append(s)
    return out


def _norm_title(v) -> str:
    return _clean(v).lower() if isinstance(v, str) else ""


_NEVER_SPOKEN = frozenset({"approval", "approvals"})   # 승인 줄은 낭독 설정이 없다 → 개수만(기본 침묵)


def _kinds(t: dict) -> set:
    """항목의 진짜 출처 종류들: source 와 병합 전 출처 태그(origin). 알 수 없는 값은 무시."""
    ks = set()
    src = t.get("source")
    if isinstance(src, str):
        ks.add(src.strip().lower())
    org = t.get("origin")
    if isinstance(org, (list, tuple)):
        ks |= {o.strip().lower() for o in org if isinstance(o, str)}
    return ks


def _source_ok(t: dict, opts: "ScriptOptions") -> bool:
    ks = _kinds(t)
    if ks & _NEVER_SPOKEN:
        return False
    if ("event" in ks or "calendar" in ks) and not opts.say_event_titles:
        return False
    return True


def _visible_todos(items, opts: "ScriptOptions | None" = None) -> list:
    """읽어도 되는 할 일. 일정·승인 출처 항목은 각자의 낭독 설정(없으면 항상 침묵)을 따른다."""
    out = []
    for t in _lst(items):
        if opts is not None and not _source_ok(t, opts):
            continue
        out.append(t)
    return out


def _compose_morning(kind: str, d: date, s: dict, opts: ScriptOptions, fri: dict | None) -> list[Sentence]:
    out = [Sentence(vc.OPEN[kind].format(date=verbal.date_ko(d)), 0)]
    friday = kind == "friday"
    if friday:
        done = _n(s.get("done_week"))
        if done:
            out.append(Sentence(vc.WEEK_DONE.format(n=verbal.count(done)), 1))
    must = _visible_todos(s.get("today_must"), opts)
    if must:
        out.append(Sentence(vc.MUST_HEAD.format(n=verbal.count(len(must))), 1))
    elif not s.get("todo_error"):
        out.append(Sentence(vc.MUST_NONE, 1))
    _items(out, [t.get("text") for t in must], opts, _n(s.get("rest_count")))
    if s.get("todo_error"):
        out.append(Sentence(vc.TODO_ERR, 1))
    _calendar(out, s, kind, opts)
    approvals = _n(s.get("approvals"))
    if approvals:
        out.append(Sentence(vc.APPROVALS.format(n=verbal.count(approvals)), 4))
    if kind == "morning" and not opts.short:
        sup = s.get("supply")
        if opts.say_supply and isinstance(sup, dict):
            due = sum(1 for it in _lst(sup.get("items")) if it.get("due"))
            if due:
                out.append(Sentence(vc.SUPPLY_DUE.format(n=verbal.count(due)), 7))
        if fri:
            fri = _allowed(fri)
            done = _n(fri.get("done_week"))
            if done:
                out.append(Sentence(vc.WEEK_DONE.format(n=verbal.count(done)), 5))
            nxt = len(_lst(fri.get("calendar")))
            if nxt and not fri.get("calendar_note"):
                out.append(Sentence(vc.NEXT_WEEK.format(n=verbal.count(nxt)), 6))
    out.append(Sentence(vc.CLOSE, 0))
    return out


def _compose_evening(d: date, s: dict, opts: ScriptOptions) -> list[Sentence]:
    out = [Sentence(vc.OPEN["evening"].format(date=verbal.date_ko(d)), 0)]
    done = len(_lst(s.get("done_today")))
    left = _visible_todos(s.get("left_today"), opts=opts)
    if done:
        out.append(Sentence(vc.DONE_TODAY.format(n=verbal.count(done)), 1))
    if left:
        out.append(Sentence(vc.LEFT_HEAD.format(n=verbal.count(len(left))), 1))
        _items(out, [t.get("text") for t in left], opts)
    elif not s.get("todo_error"):
        out.append(Sentence(vc.LEFT_NONE, 1))
    if s.get("todo_error"):
        out.append(Sentence(vc.TODO_ERR, 1))
    approvals = _n(s.get("approvals"))
    if approvals:
        out.append(Sentence(vc.APPROVALS.format(n=verbal.count(approvals)), 4))
    if not opts.short and isinstance(s.get("retro"), str) and s["retro"] in vc.RETRO_ALLOWED:
        out.append(Sentence(vc.RETRO.format(q=s["retro"]), 5))
    out.append(Sentence(vc.CLOSE, 0))
    return out


# --- 길이 예산 -----------------------------------------------------------------------------------
def _chars(sents: list[Sentence]) -> int:
    return sum(len("".join(x.text.split())) for x in sents)


def _fit_budget(sents: list[Sentence], max_chars: int, max_sentences: int) -> tuple[list[Sentence], int]:
    """prio 가 큰 문장부터(같으면 뒤쪽부터) 뺀다. 할 일(P2)은 P3 이상이 다 빠진 뒤에만 건드린다:
    먼저 제목을 15자로 줄이고, 그래도 넘치면 뒤에서부터 빼며 뺀 수만큼 '그 밖에 n건'을 다시 계산한다."""
    dropped = 0
    items_cut = 0
    shortened = False
    sents = _render_items(sents, TITLE_MAX)
    rest0 = next((s for s in sents if s.tag == "rest"), None)
    rest_base = int(rest0.prefix) if rest0 else 0

    def over() -> bool:
        return _chars(sents) > max_chars or len(sents) > max_sentences

    while over():
        cands = [i for i, s in enumerate(sents) if s.prio >= 3 and s.tag != "rest"]
        if cands:
            top = max(sents[i].prio for i in cands)
            del sents[max(i for i in cands if sents[i].prio == top)]
            dropped += 1
            continue
        item_idx = [i for i, s in enumerate(sents) if s.tag == "item"]
        if item_idx and not shortened and _chars(sents) > max_chars:
            shortened = True
            sents = _render_items(sents, TITLE_MIN)
            continue
        if item_idx:
            del sents[item_idx[-1]]
            dropped += 1
            items_cut += 1
            _refresh_rest(sents, rest_base + items_cut)
            continue
        rest = [i for i, s in enumerate(sents) if s.tag == "rest"]
        if rest:
            del sents[rest[0]]
            dropped += 1
            continue
        p1 = [i for i, s in enumerate(sents) if s.prio == 1]
        if not p1:
            break                       # P0 만 남았다 — 더 줄이지 않는다
        del sents[p1[-1]]
        dropped += 1
    return sents, dropped


def _refresh_rest(sents: list[Sentence], total: int) -> None:
    """뺀 할 일 수만큼 '그 밖에 n건'을 다시 계산해 마지막 할 일 문장 뒤에 P3 로 되살린다."""
    sents[:] = [s for s in sents if s.tag != "rest"]
    last = max((i for i, s in enumerate(sents) if s.tag == "item"), default=None)
    at = last + 1 if last is not None else next((i + 1 for i, s in enumerate(sents) if s.prio == 1), len(sents))
    sents.insert(at, Sentence(vc.REST.format(n=verbal.count(total)), 3, "rest", prefix=str(total)))


def build_script(kind: str, for_date: date, sections: dict, *, opts: ScriptOptions, friday: dict | None = None) -> VoiceScript:
    s = _allowed(sections)
    if kind == "evening":
        sents = _compose_evening(for_date, s, opts)
    elif kind in ("morning", "friday"):
        sents = _compose_morning(kind, for_date, s, opts, friday if kind == "morning" else None)
    else:
        raise ValueError(f"unknown briefing kind: {kind!r}")
    max_chars, max_sent = (MAX_CHARS_SHORT, MAX_SENTENCES_SHORT) if opts.short else (MAX_CHARS, MAX_SENTENCES)
    sents, dropped = _fit_budget(sents, max_chars, max_sent)
    lines = tuple(x.text for x in sents)
    text = " ".join(lines)
    digest = hashlib.sha256(f"{SCRIPT_VERSION}|{kind}|{for_date}|".encode() + "\n".join(lines).encode()).hexdigest()[:16]
    return VoiceScript(kind, for_date, lines, text, digest, math.ceil(_chars(sents) / CHARS_PER_SEC), dropped)


def pick_kind(available: set[str], now_kst: datetime, evening_at: time | None) -> str | None:
    """D12: 퇴근 시각 이후 + 퇴근 브리핑 있음 → evening, 아니면 morning(없으면 friday, 그것도 없으면 evening)."""
    if not available:
        return None
    local = now_kst.astimezone(KST) if now_kst.tzinfo else now_kst
    if "evening" in available and evening_at is not None and local.time() >= evening_at:
        return "evening"
    for k in ("morning", "friday", "evening"):
        if k in available:
            return k
    return None

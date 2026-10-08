"""결정적 한국어 날짜·시각 해석 (Task B1p).

LLM 은 대화 원문에서 날짜 표현 문자열만 옮기고, 실제 날짜 계산은 이 모듈이 한다.
순수 함수: I/O 없음, 시계 읽기 없음(``at`` 으로 기준 시각을 주입), 판단은 KST.
해석할 수 없거나 애매하면 **추측하지 않고 None** (호출부가 ``due_confidence='unknown'``
+ "일정 확인 필요" 로 처리).

규칙 요약(계획 B1p / D7):
  * 날짜: 오늘/금일, 내일/낼/명일, 모레, 글피, 내일모레(=모레), N일 후, 이번 주·다음 주 X요일
    (월요일 시작 주), 맨 X요일(메시지 이후 6일 안, 같은 요일이면 None),
    M월 D일, M/D, M.D, YYYY-MM-DD, YYYY년 M월 D일, D일(이번 달, 지났으면 다음 달).
    과거 M/D 는 1년 뒤가 60일 안일 때만 다음 해, 아니면 None.
  * 시각: (오전|오후|저녁|밤|새벽|아침|낮)? H시( M분| 반)?, HH:MM, 정오, 자정.
    오전/오후 없는 1~6시 → 오후(D7), 7~11시 → 오전, 12시 → 정오, 13~23시 → 24시간제.
    자정·밤 12시 = 해당 날짜의 24:00(= 다음 날 00:00). 오전 12시·새벽 12시는 애매해서 None.
    범위("2시~4시")는 시작 시각을 마감으로 쓴다(끝이 시작보다 늦어야 함).
  * 시각이 있으면 메시지 시각 이후여야 한다(오늘 이미 지난 시각 → None).
    날짜 없이 시각만 있으면 메시지 시각 이후일 때만 그날.
  * 막연·과거·반복·근사("쯤")·충돌하는 표현은 전부 None. 해석되지 않고 남는 숫자도 None.
  * ``message`` 를 주면 ``expr`` 가 원문에 실제로 있어야 한다(공백·NFKC·제로폭 문자 무시).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

MAX_EXPR_CHARS = 200
MAX_MESSAGE_CHARS = 20_000
ROLL_WINDOW_DAYS = 60

CONF_EXPLICIT = "explicit"
CONF_UNKNOWN = "unknown"

_WD = "월화수목금토일"


@dataclass(frozen=True)
class Due:
    at: datetime  # UTC aware
    all_day: bool
    label: str  # "10/08(목) 15:00" 또는 "10/08(목)"

    @property
    def due_confidence(self) -> str:
        return CONF_EXPLICIT


def due_confidence(due: Due | None) -> str:
    """할 일 due_confidence 값: 해석됐으면 'explicit', 아니면 'unknown'."""
    return CONF_EXPLICIT if due is not None else CONF_UNKNOWN


class _Unknown(Exception):
    """애매하거나 해석 불가 — 바깥에서 None 으로 바꾼다."""


# --------------------------------------------------------------------------- 정규화

def _strip_invisible(s: str) -> str:
    # Cf(제로폭 공백·ZWJ·BOM·방향 제어 등)와 공백 아닌 제어 문자 제거
    return "".join(
        ch for ch in s
        if not (unicodedata.category(ch) == "Cf" or (unicodedata.category(ch) == "Cc" and not ch.isspace()))
    )


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", _strip_invisible(s))
    s = _strip_invisible(s)
    return re.sub(r"\s+", " ", s).strip()


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", _normalize(s)).casefold()


# --------------------------------------------------------------------------- 패턴

_NOT_DAY_SUFFIX = r"(?!\s*(?:간|동안|전|후|뒤|째|치|이내)|요일|주일)"

_PAST = re.compile(
    r"어제|어저께|그제|그저께|엊그제|그끄저께|지난|작년|재작년|아까|방금|벌써|이미|전날|그글피"
)
_VAGUE = re.compile(
    r"이따|나중|조만간|다음에|담에|다음\s*번|(?<![가-힣])곧|언제|주말|주중|평일|휴일|연휴|월말|월초|연말|연초|분기"
    r"|추후|차차|언젠가|시간\s*(?:될|날|나|되면|있을|있으면)|여유|되는\s*대로|가능할|빨리|빠른|최대한|ASAP"
    r"|일주일|이틀|사흘|나흘|열흘|하루|며칠|쯤|무렵|(?<=[\d시분일])\s*경(?![가-힣])"
    r"|(?:이번|다음|담|내|지난|매)\s*달|내달|내년|올해|올\s*해|내후년|금년|명년"
    r"|오전\s*중|오후\s*중",
    re.IGNORECASE,
)
_RECURRING = re.compile(r"매\s*(?:주|일|월|달|번|년|시간)|격주|마다|정기|반복")
_HANGUL_HOUR = re.compile(
    r"(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|열한|열두)\s*시(?!간)"
)

_YMD_NUM = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_YMD_KR = re.compile(r"(?<!\d)(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일" + _NOT_DAY_SUFFIX)
_MD_KR = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일" + _NOT_DAY_SUFFIX)
_REL_N = re.compile(r"(?<!\d)(\d{1,3})\s*(일|주)\s*(?:후|뒤)")
_MD_SLASH = re.compile(
    r"(?<![\d./:\-A-Za-z])(\d{1,2})[/.](\d{1,2})(?![\d./:A-Za-z%]|\s*[시분배개명원])"
)
_D_ONLY = re.compile(r"(?<![\d./:\-])(\d{1,2})\s*일" + _NOT_DAY_SUFFIX)
_REL_WORD = re.compile(r"(?<![가-힣])(내일모레|오늘|금일|내일|낼|명일|모레|글피)")
_WEEK_WD = re.compile(
    r"(?<![가-힣])(다다음|다음|담|이번|금|차)\s*주\s*(?:에|의)?\s*([월화수목금토일])\s*요일"
)
_PAREN_WD = re.compile(r"\(\s*([월화수목금토일])\s*\)")
_BARE_WD = re.compile(r"([월화수목금토일])\s*요일")
_WEEK_WORD_LEFT = re.compile(r"(?<![가-힣])(?:다다음|다음|담|이번|금|차)\s*주")
_PERIOD_LEFT = re.compile(r"오전|오후|저녁|밤|새벽|아침|낮|정오|자정|정각(?=\d)")

_TIME = re.compile(
    r"(?:(오전|오후|저녁|밤|새벽|아침|낮)\s*)?"
    r"(?:(\d{1,2})\s*시(?!간)"
    r"(?:\s*(?:(\d{1,2})\s*분|(반)(?=$|[^가-힣]|에|까지|쯤|이|부터|정도|으로|엔|전|후)))?"
    r"|(?<![\d:.])(\d{1,2})\s*:\s*(\d{2})(?![\d:]))"
    r"|(정오|자정)"
)
_RANGE_SEP = re.compile(r"^\s*(?:[~∼〜\-–—]|부터|에서)\s*$")

_REL_WORD_DAYS = {
    "오늘": 0, "금일": 0, "내일": 1, "낼": 1, "명일": 1, "모레": 2, "내일모레": 2, "글피": 3,
}
_WEEK_OFFSET = {"이번": 0, "금": 0, "다음": 1, "담": 1, "차": 1, "다다음": 2}


# --------------------------------------------------------------------------- 날짜

def _mk(y: int, m: int, d: int) -> date:
    try:
        return date(y, m, d)
    except ValueError:
        raise _Unknown from None


def _md_to_date(m: int, d: int, today: date) -> date:
    """연도 없는 M/D. 지났으면 1년 뒤가 60일 안일 때만 다음 해."""
    try:
        cand = date(today.year, m, d)
    except ValueError:
        cand = None
    if cand is not None and cand >= today:
        return cand
    try:
        nxt = date(today.year + 1, m, d)
    except ValueError:
        raise _Unknown from None
    if (nxt - today).days <= ROLL_WINDOW_DAYS:
        return nxt
    raise _Unknown


def _day_of_month(d: int, today: date) -> date:
    if not 1 <= d <= 31:
        raise _Unknown
    if d >= today.day:
        return _mk(today.year, today.month, d)
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return _mk(y, m, d)


def _collect_dates(t: str, today: date) -> tuple[str, list[date], list[tuple[int, int | None]]]:
    dates: list[date] = []
    wds: list[tuple[int, int | None]] = []
    def sub(pattern: re.Pattern[str], text: str, fn) -> str:
        def repl(m: re.Match[str]) -> str:
            fn(m)
            return " "
        return pattern.sub(repl, text)

    def ymd(m):
        y = int(m.group(1))
        d = _mk(y, int(m.group(2)), int(m.group(3)))
        if not 2000 <= y <= 2100 or d < today:
            raise _Unknown
        dates.append(d)

    def md_kr(m):
        dates.append(_md_to_date(_chk_month(int(m.group(1))), _chk_day(int(m.group(2))), today))

    def rel_n(m):
        n = int(m.group(1)) * (7 if m.group(2) == "주" else 1)
        if n < 1 or n > 366:
            raise _Unknown
        dates.append(today + timedelta(days=n))

    def md_slash(m):
        dates.append(_md_to_date(_chk_month(int(m.group(1))), _chk_day(int(m.group(2))), today))

    def d_only(m):
        dates.append(_day_of_month(int(m.group(1)), today))

    def rel_word(m):
        dates.append(today + timedelta(days=_REL_WORD_DAYS[m.group(1)]))

    def week_wd(m):
        wds.append((_WD.index(m.group(2)), _WEEK_OFFSET[m.group(1)]))

    def paren_wd(m):
        wds.append((_WD.index(m.group(1)), None))

    def bare_wd(m):
        wds.append((_WD.index(m.group(1)), None))

    for pat, fn in (
        (_YMD_NUM, ymd), (_YMD_KR, ymd), (_MD_KR, md_kr), (_REL_N, rel_n),
        (_MD_SLASH, md_slash), (_D_ONLY, d_only), (_REL_WORD, rel_word),
        (_WEEK_WD, week_wd), (_PAREN_WD, paren_wd), (_BARE_WD, bare_wd),
    ):
        t = sub(pat, t, fn)
    return t, dates, wds


def _chk_month(m: int) -> int:
    if not 1 <= m <= 12:
        raise _Unknown
    return m


def _chk_day(d: int) -> int:
    if not 1 <= d <= 31:
        raise _Unknown
    return d


def _pick_date(dates: list[date], wds: list[tuple[int, int | None]], today: date) -> date | None:
    if len(set(dates)) > 1:
        raise _Unknown
    d = dates[0] if dates else None
    monday = today - timedelta(days=today.weekday())
    resolved: set[date] = set()
    for wd, off in wds:
        if off is None:
            if d is not None:
                if d.weekday() != wd:
                    raise _Unknown
                continue
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                raise _Unknown  # 같은 요일은 오늘인지 다음 주인지 애매
            resolved.add(today + timedelta(days=delta))
        else:
            target = monday + timedelta(days=7 * off + wd)
            if target < today:
                raise _Unknown
            if d is not None and target != d:
                raise _Unknown
            resolved.add(target)
    if d is None:
        if len(resolved) > 1:
            raise _Unknown
        d = next(iter(resolved), None)
    return d


# --------------------------------------------------------------------------- 시각

def _clock(per: str | None, h: int, m: int, pad24: bool = False) -> int:
    """분 단위(0..1440). 애매하거나 불가능하면 -1."""
    if not 0 <= m <= 59 or h < 0 or h > 24:
        return -1
    if h == 24:
        return 1440 if (m == 0 and per is None) else -1
    if per is None:
        if pad24:
            return h * 60 + m
        if 1 <= h <= 6:
            hh = h + 12  # D7
        else:
            hh = h  # 0, 7~11 오전, 12 정오, 13~23 24시간제
        return hh * 60 + m
    if per == "오전":
        hh = h if 0 <= h <= 11 else -1  # 오전 12시는 애매
    elif per == "오후":
        hh = h + 12 if 1 <= h <= 11 else (12 if h == 12 else -1)
    elif per == "낮":
        hh = h + 12 if 1 <= h <= 5 else (h if h in (11, 12) else -1)
    elif per == "저녁":
        hh = h + 12 if 4 <= h <= 11 else -1
    elif per == "아침":
        hh = h if 5 <= h <= 11 else -1
    elif per == "새벽":
        hh = h if 0 <= h <= 6 else -1
    elif per == "밤":
        if 7 <= h <= 11:
            hh = h + 12
        elif h == 12:
            return 1440 if m == 0 else -1
        else:
            hh = -1
    else:
        hh = -1
    return -1 if hh < 0 else hh * 60 + m


@dataclass
class _T:
    per: str | None
    h: int
    m: int
    pad24: bool
    special: int | None  # 정오=720, 자정=1440
    span: tuple[int, int]

    def minutes(self) -> int:
        if self.special is not None:
            return self.special
        return _clock(self.per, self.h, self.m, self.pad24)


def _collect_times(t: str) -> tuple[str, list[_T]]:
    found: list[_T] = []
    for m in _TIME.finditer(t):
        if m.group(7):
            found.append(_T(None, 0, 0, False, 720 if m.group(7) == "정오" else 1440, m.span()))
            continue
        per = m.group(1)
        if m.group(2) is not None:
            h = int(m.group(2))
            mi = int(m.group(3)) if m.group(3) else (30 if m.group(4) else 0)
            pad = False
        else:
            h, mi = int(m.group(5)), int(m.group(6))
            pad = len(m.group(5)) == 2 and m.group(5).startswith("0")
        found.append(_T(per, h, mi, pad and per is None, None, m.span()))
    blanked = _TIME.sub(" ", t)
    return blanked, found


def _pick_time(t: str, found: list[_T]) -> int | None:
    if not found:
        return None
    if len(found) > 2:
        raise _Unknown
    first = found[0].minutes()
    if first < 0:
        raise _Unknown
    if len(found) == 1:
        return first
    a, b = found
    between = t[a.span[1]:b.span[0]]
    second = b.minutes()
    if second < 0:
        raise _Unknown
    if a.special is None and b.special is None and _RANGE_SEP.match(between):
        if b.per is None and a.per is not None and not b.pad24:
            inherited = _clock(a.per, b.h, b.m)
            if inherited > first:
                second = inherited
        if second <= first:
            raise _Unknown
        return first  # 범위는 시작 시각
    if first == second:
        return first
    raise _Unknown  # 서로 다른 시각 — 충돌


# --------------------------------------------------------------------------- 본체

def _parse(t: str, base: datetime) -> Due:
    if _PAST.search(t) or _VAGUE.search(t) or _RECURRING.search(t) or _HANGUL_HOUR.search(t):
        raise _Unknown
    base_kst = base.astimezone(KST)
    today = base_kst.date()

    t, dates, wds = _collect_dates(t, today)
    between_src = t
    t, times = _collect_times(t)
    minutes = _pick_time(between_src, times)
    d = _pick_date(dates, wds, today)

    # 해석되지 않고 남은 숫자·기간어는 추측하지 않는다
    if re.search(r"\d", t) or _WEEK_WORD_LEFT.search(t) or _PERIOD_LEFT.search(t) or re.search(r"\d\s*:\s*\d", t):
        raise _Unknown
    if d is None and minutes is None:
        raise _Unknown

    if minutes is None:
        local = datetime(d.year, d.month, d.day, tzinfo=KST)
        all_day = True
    else:
        day = d if d is not None else today
        local = datetime(day.year, day.month, day.day, tzinfo=KST) + timedelta(minutes=minutes)
        all_day = False
        if local <= base_kst:
            raise _Unknown  # 이미 지난 시각
    label = f"{local:%m/%d}({_WD[local.weekday()]})"
    if not all_day:
        label += f" {local:%H:%M}"
    return Due(at=local.astimezone(UTC), all_day=all_day, label=label)


def resolve_due(expr: str | None, at: datetime, message: str | None = None) -> Due | None:
    """날짜·시각 표현 ``expr`` 를 메시지 시각 ``at`` 기준으로 해석한다.

    ``message`` 가 주어지면 ``expr`` 가 원문에 실제로 있어야 한다(환각 날짜 차단).
    해석 못 하거나 애매하면 None. ``at`` 은 tz-aware 여야 한다(아니면 ValueError).
    """
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be a timezone-aware datetime")
    if not isinstance(expr, str) or not expr or len(expr) > MAX_EXPR_CHARS:
        return None
    if message is not None:
        if not isinstance(message, str) or len(message) > MAX_MESSAGE_CHARS:
            return None
        sq = _squash(expr)
        if not sq or sq not in _squash(message):
            return None
    t = _normalize(expr)
    if not t:
        return None
    try:
        return _parse(t, at)
    except _Unknown:
        return None

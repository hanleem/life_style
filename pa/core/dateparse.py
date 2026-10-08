"""한국어 날짜·시각 표현을 코드가 직접 읽는다. 로컬 AI 는 요일 계산이 자주 틀려서, 글에 날짜 표현이 있으면 이 결과가 우선이다."""
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = "월화수목금토일"
_REL = {"오늘": 0, "내일": 1, "모레": 2, "글피": 3}
_WEEK_PREFIX = re.compile(r"(이번\s*주|다음\s*주|다다음\s*주|담\s*주)\s*([월화수목금토일])(?:요일|욜)?")
_WEEKDAY = re.compile(r"([월화수목금토일])요일")
_LAST = re.compile(r"(?:지난|저번)\s*(?:주\s*)?([월화수목금토일])(?:요일|욜)?")


def resolve_date(text: str, now: datetime) -> date | None:
    """'내일', '목요일', '다음 주 월요일' 같은 표현을 날짜로. 없으면 None. 요일만 말하면 오늘 이후 가장 가까운 그 요일."""
    today = now.astimezone(KST).date()
    m = _LAST.search(text)
    if m:   # "지난 목요일"은 과거: 오늘 이전에서 가장 가까운 그 요일
        return today - timedelta(days=(today.weekday() - WEEKDAYS.index(m.group(1))) % 7 or 7)
    m = _WEEK_PREFIX.search(text)
    if m:
        wd = WEEKDAYS.index(m.group(2))
        monday = today - timedelta(days=today.weekday())
        shift = 0 if m.group(1).startswith("이번") else (2 if m.group(1).startswith("다다음") else 1)
        return monday + timedelta(days=7 * shift + wd)
    for word, days in _REL.items():
        if word in text:
            return today + timedelta(days=days)
    m = _WEEKDAY.search(text)
    if m:
        wd = WEEKDAYS.index(m.group(1))
        return today + timedelta(days=(wd - today.weekday()) % 7 or 7)
    return None


_TIME = re.compile(r"(오전|오후|아침|저녁|낮|새벽)?\s*(\d{1,2})\s*(?:시|:)\s*(반|\d{1,2}\s*분?)?")


def find_time(text: str) -> time | None:
    """'오후 4시', '4시 반', '16:30', '16시' 를 시각으로. 접두어가 없는 1~6시는 오후로 본다(업무 시간 가정)."""
    m = _TIME.search(text)
    if not m:
        return None
    ampm, hour = m.group(1), int(m.group(2))
    if not 0 <= hour <= 24:
        return None
    if ampm in ("오후", "저녁") and hour < 12:
        hour += 12
    elif ampm is None and 1 <= hour <= 6:
        hour += 12
    minute = 0
    if m.group(3):
        minute = 30 if m.group(3) == "반" else int(re.sub(r"\D", "", m.group(3)) or 0)
    if hour == 24 or minute > 59:
        return None
    return time(hour, minute)


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_ISO = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})")
_KO = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_SLASH = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_EN1 = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.I)
_EN2 = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)


def _near_year(month: int, day: int, today: date) -> date | None:
    """연도가 없는 날짜: 올해, 이미 한 달 넘게 지났으면 내년."""
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= today - timedelta(days=30):
            return d
    return None


def absolute_dates(text: str, now: datetime) -> set[date]:
    """본문에 명시된 날짜(2026-10-15, 10월 15일, 10/15, Oct 15)만. '내일'·요일 같은 상대 표현은 포함하지 않는다."""
    today, found = now.astimezone(KST).date(), set()
    for y, m, d in _ISO.findall(text):
        try:
            found.add(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    for pat in (_KO, _SLASH):
        for m, d in pat.findall(text):
            if (nd := _near_year(int(m), int(d), today)):
                found.add(nd)
    for mon, d in _EN1.findall(text):
        if (nd := _near_year(_MONTHS[mon[:3].lower()], int(d), today)):
            found.add(nd)
    for d, mon in _EN2.findall(text):
        if (nd := _near_year(_MONTHS[mon[:3].lower()], int(d), today)):
            found.add(nd)
    return found


def dates_in(text: str, now: datetime) -> set[date]:
    """명시된 날짜 + 상대 표현(내일·다음 주 목요일 등)을 날짜 집합으로. 일정 근거 확인용."""
    found = absolute_dates(text, now)
    if (rel := resolve_date(text, now)):
        found.add(rel)
    return found


def pick_date(text: str, now: datetime, hint: date | None = None) -> date | None:
    """글이 말하는 날짜 하나. 명시된 날짜가 상대 표현·요일 단어보다 우선이다(예: '10월 23일 금요일' 은 10월 23일).
    명시된 날짜가 여럿이면 AI 가 고른 hint 가 그중 하나일 때만 그것을, 아니면 가장 이른 날짜를 쓴다."""
    explicit = absolute_dates(text, now)
    if len(explicit) == 1:
        return next(iter(explicit))
    if explicit:
        return hint if hint in explicit else min(explicit)
    return resolve_date(text, now)
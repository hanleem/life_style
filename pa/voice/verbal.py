"""숫자·건수·날짜·요일·시각을 자연스러운 한국어 낭독문으로 바꾸는 고정 표 기반 낭독기 (순수 함수, 외부 의존 없음)."""
import re
from datetime import date

_SINO = "영일이삼사오육칠팔구"
_NATIVE_ONES = ("", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉")
_NATIVE_TENS = ("", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔")
_WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
_MONTH_EXC = {6: "유월", 10: "시월"}


def sino(n: int) -> str:
    """한자어 수: 0 영, 10 십, 2026 이천이십육 (일의 자리 외 '일'은 십·백·천·만 앞에서 생략)."""
    if n < 0:
        return "마이너스 " + sino(-n)
    if n == 0:
        return "영"
    if n >= 10 ** 8:                      # 억 이상은 자리별로 읽는다 (일정 제목 속 긴 숫자 방어)
        return "".join(_SINO[int(c)] for c in str(n))
    out = ""
    if n >= 10000:
        head, n = divmod(n, 10000)
        out += ("" if head == 1 else sino(head)) + "만"
    for unit, name in ((1000, "천"), (100, "백"), (10, "십")):
        q, n = divmod(n, unit)
        if q:
            out += ("" if q == 1 else _SINO[q]) + name
    if n:
        out += _SINO[n]
    return out


def native(n: int) -> str:
    """고유어 관형형 1–99: 1 한, 2 두, 20 스무, 21 스물한. 0 은 영, 100 이상은 한자어."""
    if n == 0:
        return "영"
    if n < 0 or n >= 100:
        return sino(n)
    tens, ones = divmod(n, 10)
    if n == 20:
        return "스무"
    return _NATIVE_TENS[tens] + _NATIVE_ONES[ones]


def count(n: int, unit: str = "건") -> str:
    """'한 건', '스무 건'. 0 은 '영 건' — 0 처리는 호출부 몫."""
    return f"{native(n)} {unit}"


def weekday_ko(d: date) -> str:
    return _WEEKDAYS[d.weekday()] + "요일"


def date_ko(d: date) -> str:
    """'시월 육일 화요일' (6월 유월, 10월 시월, 일은 한자어)."""
    month = _MONTH_EXC.get(d.month) or sino(d.month) + "월"
    return f"{month} {sino(d.day)}일 {weekday_ko(d)}"


_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def time_ko(hhmm) -> str:
    """'09:30' -> '오전 아홉 시 삼십 분'. 잘못된 값은 ''. 0분은 생략."""
    m = _HHMM.match(hhmm) if isinstance(hhmm, str) else None
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return ""
    if h == 0:
        part, hour = "밤", 12
    elif h < 12:
        part, hour = "오전", h
    elif h == 12:
        part, hour = "낮", 12
    elif h < 18:
        part, hour = "오후", h - 12
    else:
        part, hour = "저녁", h - 12
    out = f"{part} {native(hour)} 시"
    if mi:
        out += f" {sino(mi)} 분"
    return out


_ISO = re.compile(r"(?<![\d.,/-])(\d{4})([-./])(\d{1,2})\2(\d{1,2})(?![\d])")
_MD_SLASH = re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})(?![\d/])")
_CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_AMPM = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?(?![A-Za-z])")
_DEC = re.compile(r"(\d+)\.(\d+)")
_NUM = re.compile(r"\d+")
_THOUSANDS = re.compile(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?!\d)")
_MINUS = re.compile(r"(?<![\w)])[-\u2212](?=\d)")
_TILDE_SI = re.compile(r"(\d+)\s*~\s*(\d+)\s*시")
_TILDE_MONTH = re.compile(r"(\d+)\s*~\s*(\d+)\s*월")
_TILDE_NUM = re.compile(r"(\d+)\s*~\s*(\d+)")
_COUNTER = re.compile(r"(\d+)\s*(시간|시|명|개|번|잔|마리|권)")
_MONTH = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])월")
_LETTER_DIGITS = re.compile(r"(?<=[A-Za-z])\d+(?:\.\d+)*")
_EN_DIGITS = ("제로", "원", "투", "쓰리", "포", "파이브", "식스", "세븐", "에이트", "나인")


def _digits(s: str) -> str:
    return "".join(_SINO[int(c)] for c in s)


def _num(s: str) -> str:
    if len(s) > 8 or (len(s) > 1 and s[0] == "0"):
        return _digits(s)
    return sino(int(s))


def _month_word(n: int) -> str:
    return _MONTH_EXC.get(n) or sino(n) + "월"


def speak_digits(text: str) -> str:
    """제목 속 숫자를 읽는 말로 바꾼다(아라비아 숫자가 하나도 남지 않는다).
    날짜(2026-10-07·2026.10.07·2026/10/07·12/25·N월), 시각(14:30·3:00 PM), 범위(10~12시·3~5월), 천 단위 쉼표, 소수, %, 음수,
    세는 말(두 명·열 시·스무 개), 영문자에 붙은 숫자(A4 → A 포)를 읽고 나머지 긴 숫자·앞자리 0 은 자리별로 읽는다."""
    def ampm(m):
        h, mi, pm = int(m.group(1)), int(m.group(2) or 0), m.group(3) in "Pp"
        if not 1 <= h <= 12 or mi > 59:
            return m.group(0)
        return time_ko(f"{h % 12 + (12 if pm else 0):02d}:{mi:02d}")

    def tilde_si(m):
        a, b = int(m.group(1)), int(m.group(2))
        return f"{native(a)} 시에서 {native(b)} 시" if 0 < a < 100 and 0 < b < 100 else f"{_num(m.group(1))}에서 {_num(m.group(2))} 시"

    def tilde_month(m):
        a, b = int(m.group(1)), int(m.group(2))
        if not (1 <= a <= 12 and 1 <= b <= 12):
            return f"{_num(m.group(1))}에서 {_num(m.group(2))}월"
        first = _MONTH_EXC.get(a) or sino(a)
        return f"{first}에서 {_month_word(b)}"

    def iso(m):
        try:
            return date_ko(date(int(m.group(1)), int(m.group(3)), int(m.group(4))))
        except ValueError:
            return _digits(m.group(1)) + _digits(m.group(3)) + _digits(m.group(4))

    def md(m):
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{_month_word(mo)} {sino(d)}일"
        return f"{_num(m.group(1))} 분의 {_num(m.group(2))}"

    def counter(m):
        s, unit = m.group(1), m.group(2)
        n = int(s)
        word = native(n) if 0 < n < 100 and s[0] != "0" else _num(s)
        return f"{word} {unit}"

    def letter_digits(m):
        return " " + " ".join("점" if c == "." else _EN_DIGITS[int(c)] for c in m.group(0))

    text = _AMPM.sub(ampm, text)
    text = _TILDE_SI.sub(tilde_si, text)
    text = _TILDE_MONTH.sub(tilde_month, text)
    text = _TILDE_NUM.sub(lambda m: f"{_num(m.group(1))}에서 {_num(m.group(2))}", text)
    text = _THOUSANDS.sub(lambda m: m.group(0).replace(",", ""), text)
    text = _ISO.sub(iso, text)
    text = _MD_SLASH.sub(md, text)
    text = _MINUS.sub("마이너스 ", text)
    text = _CLOCK.sub(lambda m: time_ko(m.group(0)) or _digits(m.group(1)) + " " + _digits(m.group(2)), text)
    text = _LETTER_DIGITS.sub(letter_digits, text)
    text = _DEC.sub(lambda m: f"{_num(m.group(1))} 점 {_digits(m.group(2))}", text)
    text = _COUNTER.sub(counter, text)
    text = _MONTH.sub(lambda m: _month_word(int(m.group(1))), text)
    text = re.sub(r"(\d+)\s*원", lambda m: f"{_num(m.group(1))} 원", text)
    text = _NUM.sub(lambda m: _num(m.group(0)), text)
    text = text.replace("%", " 퍼센트 ").replace("~", " 에서 ")
    return " ".join(text.split())

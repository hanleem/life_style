"""고정 낭독 문구와 민감어 판정. 낭독 문장은 여기의 상수 + 낭독기 + (가림·정리된) 제목만으로 만든다 (LLM 없음).

주의(실제 확인한 함정): '첫째·둘째'는 coach_copy.STREAK_RE 의 `\\S*째` 에 걸리고, '다음 주'는 공백을 지우면 '다음주'→'음주'가 된다.
그래서 서수·'약 n초' 같은 표현을 쓰지 않는다. 모든 상수는 tests/test_voice_privacy.py 가 violations/notify_safe 로 검사한다.
"""
import re
import unicodedata

from pa.core import briefing, coach_copy

OPEN = {"morning": "{date}, 아침 브리핑입니다.", "evening": "{date}, 저녁 브리핑입니다.", "friday": "{date}, 주간 정리 브리핑입니다."}
MUST_HEAD = "오늘 꼭 할 일은 {n}입니다."
MUST_NONE = "오늘 꼭 할 일로 정한 것은 없습니다."
ITEM_PREFIXES = ("먼저", "다음으로", "그리고", "또")
ITEM = "{prefix}, {title}."
REST = "그 밖에 {n}이 더 있습니다."
CAL_HEAD = {"morning": "오늘 일정은 {n}입니다.", "friday": "다음 한 주 일정은 {n}입니다."}
CAL_ITEM = "{time}, {title}."
CAL_TITLE_ONLY = "{title}."
CAL_ALLDAY = "종일, {title}."
CAL_TIME = "{when}에 일정이 있습니다."                       # 제목 낭독 꺼짐(기본): 제목 없이 시각만
CAL_ALLDAY_ONLY = "{when} 종일 일정이 있습니다."
CAL_ALLDAY_PLAIN = "종일 일정이 있습니다."
PRIVATE_EVENTS = "개인 일정 {n}은 읽지 않습니다."            # 가려진 일정은 제목·시각 없이 이 한 문장으로만
PRIVATE_TODOS = "개인 항목 {n}은 읽지 않습니다."
CAL_MORE = "나머지 {n}은 화면에서 확인해 주세요."
CAL_NONE = "오늘 일정은 없습니다."
NEXT_WEEK_NONE = "다음 한 주 일정은 없습니다."
CAL_ERR = "일정을 불러오지 못했습니다."
TODO_ERR = "할 일을 불러오지 못했습니다."
APPROVALS = "승인을 기다리는 일이 {n} 있습니다."
SUPPLY_DUE = "주문 확인이 필요한 항목이 {n} 있습니다."
DONE_TODAY = "오늘 마친 일은 {n}입니다."
LEFT_HEAD = "남은 일은 {n}입니다."
LEFT_NONE = "남은 일은 없습니다."
RETRO = "오늘의 질문입니다. {q}"
WEEK_DONE = "지난 칠 일 동안 마친 일은 {n}입니다."
NEXT_WEEK = CAL_HEAD["friday"]
PRIVATE_ITEM = "개인 항목"
CLOSE = "자세한 내용은 화면에서 확인해 주세요."

UI = {"play": "🔊 오늘 브리핑 듣기 (1분)", "morning_again": "🔁 아침 브리핑 다시 듣기", "loading": "준비 중…", "pause": "일시정지", "resume": "이어 듣기", "stop": "정지",
      "again": "▶ 재생", "no_briefing": "아직 오늘 브리핑이 없습니다", "disabled": "음성 브리핑이 꺼져 있습니다",
      "error": "소리를 만들 수 없어 글로 보여 드립니다", "rate": "속도", "text": "읽는 내용 보기", "est": "{s}초 정도"}

# 설정 화면(/settings/voice) 문구 — 소리로 읽지 않는 화면 전용 고정 문구.
SENSITIVE_WARNING = ("제목에 민감한 단어가 들어 있을 수 있고, 가리는 단어 목록은 완전할 수 없습니다. "
                     "주변에 사람이 있는 곳에서는 꺼 두세요.")
LOCAL_ONLY = "이 PC에서만 소리를 만듭니다"
SETTINGS_COPY = {
    "title": "음성 브리핑 설정",
    "enabled": "음성 브리핑 사용",
    "engine": "음성 엔진",
    "rate": "재생 속도",
    "say_todo_titles": "할 일 제목 읽기",
    "say_event_titles": "일정 제목 읽기",
    "say_supply": "식사 공급 항목 읽기",
    "why_say_todo_titles": "기본은 꺼 둡니다. 끄면 할 일은 개수만 읽습니다.",
    "why_say_event_titles": "기본은 꺼 둡니다. 끄면 일정은 개수와 시각만 읽습니다.",
    "why_say_supply": "기본은 꺼 둡니다. 식사·건강과 가까운 정보라서, 켜도 주문 확인이 필요한 항목의 개수만 읽습니다.",
    "engine_windows": "Windows 내장 한국어 음성",
    "engine_none": "사용 안 함 (글로 보기)",
    "save": "저장",
    "saved": "저장했습니다",
    "back": "홈으로",
}

ALL_TEMPLATES: tuple[str, ...] = (
    *OPEN.values(), MUST_HEAD, MUST_NONE, *ITEM_PREFIXES, ITEM, REST, *CAL_HEAD.values(), CAL_ITEM,
    CAL_TITLE_ONLY, CAL_ALLDAY, CAL_TIME, CAL_ALLDAY_ONLY, CAL_ALLDAY_PLAIN, PRIVATE_EVENTS, PRIVATE_TODOS, CAL_MORE, CAL_NONE, NEXT_WEEK_NONE, CAL_ERR, TODO_ERR, APPROVALS,
    SUPPLY_DUE, DONE_TODAY, LEFT_HEAD, LEFT_NONE, RETRO, WEEK_DONE, NEXT_WEEK,
    PRIVATE_ITEM, CLOSE,
)

# D11: 퇴근 브리핑에서 읽어도 되는 회고 질문 = briefing 의 고정 질문 중 coach 문구 검사를 통과하는 것만.
# (고정 질문 중 '이유'가 든 문장은 coach_copy 인과 표현 검사에 걸려 제외된다.)
RETRO_ALLOWED: frozenset[str] = frozenset(
    q for q in briefing._RETRO if coach_copy.violations(q) == [] and coach_copy.notify_safe(q))

# D13: 알림 금칙어에서 한 글자 단어(술·잠·약·kg)를 빼고(문맥 규칙으로 대체) 상담·진료 등을 더한다. 오탐(가림 과다)은 안전한 쪽.
# 이 목록은 절대 완전할 수 없다: 그래서 일정·할 일 제목은 기본으로 읽지 않고(D4·D5), 사용자가 켠 경우의 보조 방어선으로만 쓴다.
_SINGLE = ("술", "잠", "약", "kg")
SENSITIVE_TERMS: tuple[str, ...] = tuple(w for w in coach_copy.FORBIDDEN_NOTIFY_WORDS if w not in _SINGLE) + (
    "수면제", "음주", "술자리", "술 약속", "복약", "처방", "약 먹", "약 복용", "진료", "검진", "치료", "정신과", "상담", "심리",
    "클리닉", "일기", "회고", "감주", "단주", "금연", "생리", "임신", "산부인과", "비뇨", "내과", "치과", "한의원",
    # 의료
    "안과", "피부과", "이비인후과", "정형외과", "신경과", "소아과", "성형외과", "재활", "내시경", "위내시경", "피검사", "혈액",
    "mri", "초음파", "접종", "백신", "주사", "공황", "adhd", "트라우마", "병가", "스케일링", "임플란트", "인바디", "혈압",
    "체지방", "약국", "헬스", "명상", "코칭", "고민", "hiv", "코로나", "성병", "탈모", "피부",
    # 법률·금융
    "소송", "법원", "재판", "이혼", "변호사", "경찰", "대출", "연체", "채무", "파산",
    # 관계·애도·휴직
    "소개팅", "데이트", "장례", "문상", "조문", "휴직",
    # 영어(소문자)
    "dentist", "doctor", "clinic", "hospital", "therap", "psychiatr", "psycholog", "counsel", "rehab", "blood test",
    "checkup", "vaccine", "aa meeting")
# 독립 단어일 때만(뒤에 조사만 허용): '약속'·'요약'·'기술'·'잠정'은 걸리지 않고 '술 약속'·'약을'·'잠이'는 걸린다.
SENSITIVE_RE = re.compile(
    r"(?<![가-힣])(?:술|잠|약)(?:으로|을|를|이|가|은|는|도|과|와|에|로|만|의)?(?![가-힣])|\d+\s*kg"
    r"|(?<![a-z0-9])(?:aa|pt)(?![a-z0-9])")
_NEXT_WEEK = re.compile(r"다음\s*주")             # 공백을 지우면 '다음주' → '음주' 가 되는 알려진 오탐
_SQUEEZED_TERMS = tuple(t.replace(" ", "") for t in SENSITIVE_TERMS)


def fold(text: str) -> str:
    """NFKC, 제어 공백은 띄어쓰기로, 나머지 제어·서식 문자(제로폭·소프트하이픈 등) 제거, 소문자."""
    t = unicodedata.normalize("NFKC", text)
    t = "".join(" " if c in "\t\n\r\x0b\x0c" else c for c in t)
    return "".join(c for c in t if unicodedata.category(c) not in ("Cf", "Cc")).lower()


def is_sensitive(text: str) -> bool:
    """제목이 민감한지. 모든 단어(2자 포함)를 공백을 지운 사본에서도 찾는다('치 과'·'병 원'·'음  주'). 다만 알려진 오탐
    '다음 주'는 먼저 지운다. 오탐(예: '재활용'·'의사결정'·'상담 일정')은 안전한 쪽의 비용으로 받아들인다."""
    t = _NEXT_WEEK.sub(" ", fold(text or ""))
    if any(w in t for w in SENSITIVE_TERMS) or SENSITIVE_RE.search(t):
        return True
    squeezed = "".join(t.split())
    return any(w in squeezed for w in _SQUEEZED_TERMS)

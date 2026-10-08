"""고정 안내 문구(LLM 생성 없음)와 알림 문구 검사기. 물품 공급(식사 재고) 화면에서 쓴다."""
import re
import unicodedata

CUTOFF_QUESTION = "주문 마감 시간이 있나요? (예: 19:00, 없으면 건너뛰기)"
ROTATION_PROMPT = "이번 달은 구성을 바꿔볼까요?"
ROTATION_EMPTY = "후보 링크를 추가해 주세요"
STOCK_CHECK = "남은 끼니를 확인해 주세요"
REMAINING_LABEL = "예상 남은 끼니"
CUTOFF_HINT = "오늘 {hhmm} 전"
LEAD_PROPOSAL = "최근 {n}번 주문은 도착까지 보통 {days}일 걸렸어요. 도착 일수를 {days}일로 바꿀까요?"
PROPOSAL_ACTIONS = ("수락", "고치기", "오늘은 패스")
UNDO_LABEL = "되돌리기"
SKIP_LABEL = "건너뛰기"
GENERIC_ERROR = "잠시 후 다시 시도해 주세요"

# 알림에 들어가면 안 되는 민감 단어(알림은 고정 문구만 쓴다)
FORBIDDEN_NOTIFY_WORDS: tuple[str, ...] = (
    "술", "음주", "알코올", "마심", "마셨", "금주", "수면", "잠", "깼", "취침", "기상", "불면",
    "기분", "우울", "불안", "컨디션", "체중", "몸무게", "kg", "다이어트", "칼로리", "kcal",
    "건강", "병원", "의사", "약", "위기", "자살",
    "알콜", "맥주", "소주", "숙취", "과음", "한잔", "통증", "스트레스",
)
STREAK_RE = re.compile(r"연속|연달아|스트릭|streak|\d+\s*일\s*째|최장|기록\s*(을\s*)?깨|일차|\S*째|이어가|신기록|기록이?\s*(깨|끊)"
                       r"|내내|한\s*번도|계속\s*(잘\s*)?(지키|안)|\d+\s*일\s*(동안|간)\s*(계속|모두)", re.I)
BLAME_RE = re.compile(r"실패|못\s*지켰|어겼|또\s*마셨|왜\s*마셨|반성|의지\s*(가\s*)?부족|잘못|참았어야|그만\s*마셔|다시\s*마셨|지키지\s*(못|않)")
CAUSAL_RE = re.compile(r"때문|탓|원인|(으)?로\s*인해|덕분|결과적으로|영향|초래|유발|인한|이유|그래서")


def _normalized(text: str) -> str:
    """NFKC 로 정규화하고 제어·서식 문자(제로폭 등)를 제거한다."""
    return "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) not in ("Cf", "Cc"))


def violations(text: str) -> list[str]:
    norm = _normalized(text)
    variants = (norm, "".join(norm.split()))   # 공백을 끼워 넣은 우회("연 속")도 잡는다
    out = []
    for name, rx in (("streak", STREAK_RE), ("blame", BLAME_RE), ("causal", CAUSAL_RE)):
        if any(rx.search(v) for v in variants):
            out.append(name)
    return out


def notify_safe(text: str) -> bool:
    low = "".join(c for c in unicodedata.normalize("NFKC", text).lower()
                  if not c.isspace() and unicodedata.category(c) not in ("Cf", "Cc"))
    return not any(w in low for w in FORBIDDEN_NOTIFY_WORDS)


NOTIFY_TITLES = {"routine_supply": "이번 주 주문 확인"}
SUPPLY_BRIEF_DUE = NOTIFY_TITLES["routine_supply"]
SUPPLY_BRIEF_HEADING = "## 식사 공급"

DASH_SUPPLY_LINE = REMAINING_LABEL + " {n}끼"
DASH_SUPPLY_NONE = "공급 항목을 추가해 주세요"
DASH_SUPPLY_BADGE = "주문 확인"

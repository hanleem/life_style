"""가족 프로필(첫 인터뷰의 답). `data/family_profile.yaml` 한 파일에 저장한다(저장소에는 올라가지 않는 data/ 아래).

없으면 앱의 모든 화면이 /welcome 으로 넘어간다. 화면·말투·식단에 쓰는 값은 여기서만 읽는다.
"""
import os
import tempfile
from pathlib import Path

import yaml

FILENAME = "family_profile.yaml"
TONES = {"friendly": "친근한 말투", "polite": "존댓말"}
SKILLS = {"beginner": "초보", "intermediate": "보통", "advanced": "능숙"}
COOK_MINUTES = (20, 30, 45, 60)
SHOPS = {"coupang": "쿠팡", "mart": "마트", "kurly": "컬리", "other": "기타"}
INTERESTS = {"kids_edu": "아이 교육·준비물", "housekeeping": "집안일·생필품", "health": "가족 건강",
             "money": "생활비", "outing": "주말 나들이", "hobby": "취미"}
THEMES = {"pastel": "파스텔", "wood": "우드", "mono": "모노", "fresh": "프레시"}
FONT_SIZES = {"normal": "보통", "large": "크게"}
ROLES = {"adult": "어른", "child": "아이"}
SEXES = {"m": "남", "f": "여"}
MAX_LIST = 20
MAX_MEMBERS = 8


class ProfileError(ValueError):
    """사용자에게 보여 줄 수 있는 쉬운 한국어 메시지를 담는다."""


def defaults() -> dict:
    return {"honorific": "", "tone": "polite",
            "members": [{"role": "adult", "name": "", "age": 40, "sex": "f"}],
            "allergies": [], "dislikes": [], "cook_skill": "intermediate", "weekday_cook_min": 30,
            "weekly_budget_won": 200000, "shops": ["coupang"], "interests": [],
            "theme": "fresh", "font_size": "normal"}


def _choice(v, allowed, label: str) -> str:
    if v not in allowed:
        raise ProfileError(f"{label}을(를) 다시 골라 주세요")
    return v


def _int(v, label: str, lo: int, hi: int) -> int:
    try:
        n = int(str(v).strip().replace(",", ""))
    except ValueError:
        raise ProfileError(f"{label}은(는) 숫자로 적어 주세요") from None
    if not lo <= n <= hi:
        raise ProfileError(f"{label}은(는) {lo}에서 {hi} 사이로 적어 주세요")
    return n


def _words(v) -> list[str]:
    if isinstance(v, str):
        v = v.replace("\n", ",").split(",")
    out: list[str] = []
    for w in v or []:
        w = str(w).strip()[:20]
        if w and w not in out:
            out.append(w)
    return out[:MAX_LIST]


def _members(rows) -> list[dict]:
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not (str(r.get("role") or "").strip() or str(r.get("age") or "").strip()):
            continue   # 빈 줄은 건너뛴다
        role = _choice(str(r.get("role") or "").strip(), ROLES, "가족 구성(어른·아이)")
        age = _int(r.get("age"), "나이", 0 if role == "child" else 19, 18 if role == "child" else 120)
        out.append({"role": role, "name": str(r.get("name") or "").strip()[:10], "age": age,
                    "sex": _choice(str(r.get("sex") or "").strip(), SEXES, "성별")})
    if not out:
        raise ProfileError("가족을 한 명 이상 적어 주세요")
    return out[:MAX_MEMBERS]


def normalize(a: dict) -> dict:
    """답 전체를 검사해 저장 형식으로 만든다. 모르는 키는 버린다."""
    honorific = str(a.get("honorific") or "").strip()
    if len(honorific) > 20:
        raise ProfileError("부를 이름은 20자 안으로 적어 주세요")
    shops = [s for s in (a.get("shops") or [])]
    if not shops or any(s not in SHOPS for s in shops):
        raise ProfileError("장보는 곳을 한 곳 이상 골라 주세요")
    interests = list(a.get("interests") or [])
    if any(i not in INTERESTS for i in interests):
        raise ProfileError("관심사를 다시 골라 주세요")
    return {"honorific": honorific, "tone": _choice(a.get("tone"), TONES, "말투"),
            "members": _members(a.get("members")), "allergies": _words(a.get("allergies")),
            "dislikes": _words(a.get("dislikes")),
            "cook_skill": _choice(a.get("cook_skill"), SKILLS, "요리 실력"),
            "weekday_cook_min": _int(a.get("weekday_cook_min"), "평일 요리 시간", 5, 180),
            "weekly_budget_won": _int(a.get("weekly_budget_won"), "주간 식비", 0, 5_000_000),
            "shops": list(dict.fromkeys(shops)), "interests": list(dict.fromkeys(interests)),
            "theme": _choice(a.get("theme"), THEMES, "테마"),
            "font_size": _choice(a.get("font_size"), FONT_SIZES, "글씨 크기")}


def path(data_dir: Path) -> Path:
    return Path(data_dir) / FILENAME


def exists(data_dir: Path) -> bool:
    return load(data_dir) is not None


_cache: dict[str, tuple[float, dict | None]] = {}


def load(data_dir: Path) -> dict | None:
    """프로필을 읽는다. 없거나 깨졌으면 None(→ 인터뷰). 파일 수정 시각으로 캐시한다."""
    p = path(data_dir)
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return None
    hit = _cache.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        prof = normalize(raw) if isinstance(raw, dict) else None
    except (OSError, yaml.YAMLError, ProfileError):
        prof = None
    _cache[str(p)] = (mtime, prof)
    return prof


def save(data_dir: Path, answers: dict) -> dict:
    """검사한 뒤 한 번에 저장한다(쓰다 끊겨도 옛 파일이 남는다)."""
    prof = normalize(answers)
    d = Path(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".profile-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump(prof, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, path(d))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _cache.pop(str(path(d)), None)
    return prof


def theme_attrs(prof: dict | None) -> dict:
    """<html data-theme data-font> 에 넣을 값. 알 수 없는 값은 기본으로 되돌린다(화면 깨짐·주입 방지)."""
    p = prof or {}
    return {"theme": p.get("theme") if p.get("theme") in THEMES else "fresh",
            "font": p.get("font_size") if p.get("font_size") in FONT_SIZES else "normal"}

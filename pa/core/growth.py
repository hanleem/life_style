"""성장기 아이를 위한 주간 영양 체크. AI 가 아니라 코드로 판정한다(식단 재료 이름을 식품군으로 분류).

기준(한 주): 칼슘원 7끼 이상, 철분원 4끼 이상, 생선 2끼 이상, 저녁·주말 점심마다 채소 반찬 2가지 이상, 단 음료·튀김 2끼 이하.
'끼'는 아침·점심·저녁·간식 한 번을 말한다(일요일 밑반찬 만들기는 세지 않는다). 의학적 진단이 아니라 식단 균형을 보는 참고 기준이다.
"""
THRESHOLDS = {"calcium": 7, "iron": 4, "fish": 2, "veg_sides": 2, "junk": 2}
LABELS = {"calcium": "칼슘이 든 끼니(우유·요거트·치즈·멸치·두부)", "iron": "철분이 든 끼니(소고기·달걀·시금치)",
          "fish": "생선이 든 끼니", "veg_sides": "채소 반찬 2가지 이상인 저녁·주말 점심", "junk": "단 음료·튀김 끼니"}
_KEYWORDS = {
    "calcium": ("우유", "요거트", "요구르트", "치즈", "멸치", "두부"),
    "iron": ("소고기", "쇠고기", "불고기", "달걀", "계란", "시금치"),
    "fish": ("생선", "고등어", "연어", "삼치", "대구", "갈치", "조기", "광어", "가자미", "명태", "동태", "꽁치", "참치",
             "임연수", "도미", "장어"),
    "junk": ("탄산", "콜라", "사이다", "주스", "에이드", "음료수", "튀김", "튀긴", "치킨", "돈가스", "돈까스", "프라이드",
             "핫도그", "너겟", "탕수육", "강정"),
}
_MEAL_SLOTS = ("breakfast", "lunch", "dinner", "snack")
_SIDE_SLOTS = ("dinner", "lunch")


def _text(m: dict) -> str:
    ings = m.get("ingredients") if isinstance(m.get("ingredients"), list) else []
    names = [str(i.get("name") or "") for i in ings if isinstance(i, dict)]
    return (str(m.get("title") or "") + " " + " ".join(names)).replace(" ", "")


def groups(m: dict) -> set[str]:
    """한 끼가 속하는 식품군 집합. 이름에 키워드가 들어 있으면 그 군에 넣는다."""
    t = _text(m)
    return {g for g, words in _KEYWORDS.items() if any(w in t for w in words)}


def evaluate(plan) -> dict:
    """주간 식단 한 개를 판정한다. {ok, checks:[{key,label,got,need,ok,kind}], failed:[key]}"""
    ms = [m for m in (plan or {}).get("meals") or [] if isinstance(m, dict) and m.get("slot") in _MEAL_SLOTS] \
        if isinstance(plan, dict) else []
    counts = {g: sum(1 for m in ms if g in groups(m)) for g in ("calcium", "iron", "fish", "junk")}
    sided = [m for m in ms if m.get("slot") in _SIDE_SLOTS]
    good_sides = sum(1 for m in sided if isinstance(m.get("veg_sides"), list)
                     and len([s for s in m["veg_sides"] if str(s).strip()]) >= THRESHOLDS["veg_sides"])
    checks = [{"key": k, "label": LABELS[k], "got": counts[k], "need": THRESHOLDS[k], "kind": "min",
               "ok": counts[k] >= THRESHOLDS[k]} for k in ("calcium", "iron", "fish")]
    checks.append({"key": "veg_sides", "label": LABELS["veg_sides"], "got": good_sides, "need": len(sided), "kind": "min",
                   "ok": bool(sided) and good_sides >= len(sided)})
    checks.append({"key": "junk", "label": LABELS["junk"], "got": counts["junk"], "need": THRESHOLDS["junk"], "kind": "max",
                   "ok": counts["junk"] <= THRESHOLDS["junk"]})
    failed = [c["key"] for c in checks if not c["ok"]]
    return {"ok": bool(ms) and not failed, "checks": checks, "failed": failed}


_FIX = {"calcium": "칼슘이 든 끼니가 {got}끼뿐이다. 우유·요거트·치즈·멸치·두부를 써서 {need}끼 이상으로 늘린다",
        "iron": "철분이 든 끼니가 {got}끼뿐이다. 소고기·달걀·시금치를 써서 {need}끼 이상으로 늘린다",
        "fish": "생선이 든 끼니가 {got}끼뿐이다. 생선 요리를 {need}끼 이상 넣는다",
        "veg_sides": "채소 반찬이 2가지 미만인 저녁이 있다(충족 {got}/{need}끼). 모든 저녁에 채소 반찬을 2가지 이상 적는다",
        "junk": "단 음료·튀김이 {got}끼다. {need}끼 이하로 줄인다"}


def followup_lines(report: dict) -> list[str]:
    """다시 만들 때 프롬프트에 붙일 보완 지시(못 채운 항목만). 모두 충족이면 빈 목록."""
    bad = [c for c in report["checks"] if not c["ok"]]
    if not bad:
        return []
    return ["[성장 영양 보완 — 이전 식단에서 부족했던 점]"] + [f"- {_FIX[c['key']].format(**c)}" for c in bad] + ["[/성장 영양 보완]"]


def badge(report: dict) -> tuple[str, str]:
    """('ok'|'warn', 화면에 보일 문구)."""
    if report["ok"]:
        return "ok", "이번 주 성장 영양 체크 · 모두 충족"
    return "warn", f"이번 주 성장 영양 체크 · {len(report['failed'])}가지 부족"

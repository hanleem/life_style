"""다이어트 식단용 칼로리 환산: 단백질·탄수화물·지방이 많은 식자재만 100g당 열량을 알고 있고(추정값), 한 끼 목표 칼로리에서
그 재료가 맡는 몫으로 '1인분 몇 g'을 정한다. 식단·재고의 인분은 이 값으로 환산한다.
- 값은 흔한 식품의 대략값이다(조리·부위·브랜드에 따라 다르다). 건강·체중에 대한 효능 주장이나 의학적 조언이 아니라 환산 편의용이다.
- 채소·양념처럼 열량 비중이 작은 재료는 다루지 않는다(기존 방식 그대로)."""
import re

# (이름 조각, 100g당 kcal, 단백질 g, 탄수화물 g, 지방 g, 분류) — 분류: protein / carb / fat
_SEED: list[tuple[str, int, float, float, float, str]] = [
    ("닭가슴", 110, 23.0, 0.0, 1.5, "protein"), ("닭다리", 150, 19.0, 0.0, 8.0, "protein"), ("닭", 140, 20.0, 0.0, 6.0, "protein"),
    ("돼지뒷다리", 150, 21.0, 0.0, 7.0, "protein"), ("뒷다리", 150, 21.0, 0.0, 7.0, "protein"), ("앞다리", 190, 19.0, 0.0, 12.0, "protein"),
    ("삼겹", 330, 14.0, 0.0, 30.0, "protein"), ("돼지", 170, 20.0, 0.0, 10.0, "protein"), ("소고기", 190, 20.0, 0.0, 12.0, "protein"),
    ("연어", 200, 20.0, 0.0, 13.0, "protein"), ("참치", 130, 26.0, 0.0, 2.0, "protein"), ("새우", 90, 19.0, 0.5, 1.0, "protein"),
    ("생선", 120, 20.0, 0.0, 4.0, "protein"), ("계란", 145, 12.5, 1.0, 10.0, "protein"), ("달걀", 145, 12.5, 1.0, 10.0, "protein"),
    ("두부", 85, 8.0, 2.0, 5.0, "protein"), ("병아리콩", 165, 9.0, 27.0, 2.6, "protein"), ("렌틸", 115, 9.0, 20.0, 0.4, "protein"),
    ("즉석밥", 150, 2.7, 33.0, 0.3, "carb"), ("쌀밥", 150, 2.7, 33.0, 0.3, "carb"), ("밥", 150, 2.7, 33.0, 0.3, "carb"),
    ("현미", 150, 3.0, 32.0, 1.0, "carb"), ("소바", 130, 5.0, 26.0, 0.5, "carb"), ("우동", 130, 3.5, 27.0, 0.5, "carb"),
    ("파스타", 150, 5.5, 30.0, 1.0, "carb"), ("스파게티", 150, 5.5, 30.0, 1.0, "carb"), ("면", 135, 4.5, 27.0, 0.8, "carb"),
    ("감자", 77, 2.0, 17.0, 0.1, "carb"), ("고구마", 86, 1.6, 20.0, 0.1, "carb"), ("빵", 265, 9.0, 49.0, 3.0, "carb"),
    ("올리브오일", 884, 0.0, 0.0, 100.0, "fat"), ("참기름", 884, 0.0, 0.0, 100.0, "fat"), ("식용유", 884, 0.0, 0.0, 100.0, "fat"),
    ("버터", 717, 0.9, 0.1, 81.0, "fat"), ("마요네즈", 680, 1.0, 3.0, 75.0, "fat"), ("아보카도", 160, 2.0, 9.0, 15.0, "fat"),
    ("견과", 600, 15.0, 18.0, 50.0, "fat"), ("치즈", 330, 22.0, 2.0, 26.0, "fat"),
]
DEFAULT_MEAL_KCAL = 600   # 한 끼 목표 칼로리 기본값(config/meals.yaml 의 meal_kcal 로 바꾼다). TODO(Stage 4): 가족 구성원별 목표로 교체
DEFAULT_SHARES = {"protein": 0.35, "carb": 0.45, "fat": 0.10}   # 한 끼 칼로리 중 그 분류의 재료가 맡는 몫(나머지는 채소·양념)
_SPECIAL_G = (("계란", 50), ("달걀", 50), ("두부", 300), ("즉석밥", 130), ("밥", 130), ("바나나", 100))   # 개·모·공기를 g 으로 바꿀 때


def lookup(name: str):
    """재료 이름에 맞는 영양 항목(가장 긴 이름 조각 우선). 없으면 None — 단백질·탄수화물·지방이 많은 재료만 있다."""
    name = (name or "").replace(" ", "")
    hits = [e for e in _SEED if e[0] in name]
    return max(hits, key=lambda e: len(e[0])) if hits else None


def kcal_per_100g(name: str) -> int | None:
    e = lookup(name)
    return e[1] if e else None


def parse_grams(name: str, qty: str) -> float | None:
    """'150g', '1.5kg', '2개'(계란 등), '1모'(두부), '1공기'(밥) 같은 양을 g 으로. 모르면 None(지어내지 않는다)."""
    q = (qty or "").replace(",", "").strip().lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g)\b", q) or re.search(r"(\d+(?:\.\d+)?)\s*(kg|g)", q)
    if m:
        return float(m.group(1)) * (1000 if m.group(2) == "kg" else 1)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(개|모|공기|알|팩|봉)", q)
    if m:
        for key, g in _SPECIAL_G:
            if key in (name or ""):
                return float(m.group(1)) * g
    return None


def serving_grams(name: str, meal_kcal: float = DEFAULT_MEAL_KCAL, shares: dict | None = None) -> float | None:
    """1인분(= 한 끼 칼로리 중 이 재료가 맡는 몫을 채우는 양)이 몇 g 인지. 영양 정보가 없는 재료는 None."""
    e = lookup(name)
    if e is None:
        return None
    share = (shares or DEFAULT_SHARES).get(e[5], 0.2)
    return round(meal_kcal * share / e[1] * 100, 1)


def servings_for(name: str, grams: float, meal_kcal: float = DEFAULT_MEAL_KCAL, shares: dict | None = None) -> float | None:
    """grams 가 대략 몇 인분인지(소수 한 자리). 영양 정보가 없으면 None."""
    sg = serving_grams(name, meal_kcal, shares)
    return round(grams / sg, 1) if sg else None


def meal_kcal(meal: dict) -> int | None:
    """한 끼의 열량 추정: 양을 g 으로 읽을 수 있고 영양 정보가 있는 재료(단백질·탄수화물·지방이 많은 것)만 더한다.
    하나도 못 읽으면 None. 채소·양념은 빠지므로 '주요 식재료 기준'의 대략값이다."""
    total, seen = 0.0, False
    for i in meal.get("ingredients") or []:
        e = lookup(str(i.get("name") or ""))
        g = parse_grams(str(i.get("name") or ""), str(i.get("qty") or ""))
        if e and g:
            total += e[1] * g / 100
            seen = True
    return round(total) if seen else None


def rules(cfg: dict | None) -> tuple[float, dict]:
    """설정(meals.yaml prefs)에서 한 끼 목표 칼로리와 분류별 몫을 읽는다."""
    prefs = (cfg or {}).get("prefs") or {}
    try:
        kcal = float(prefs.get("meal_kcal") or DEFAULT_MEAL_KCAL)
    except (TypeError, ValueError):
        kcal = DEFAULT_MEAL_KCAL
    shares = dict(DEFAULT_SHARES)
    for k, v in (prefs.get("calorie_shares") or {}).items():
        try:
            shares[str(k)] = float(v)
        except (TypeError, ValueError):
            pass
    return kcal, shares


def prompt_lines(cfg: dict | None) -> list[str]:
    kcal, shares = rules(cfg)
    ref = ", ".join(f"{n} {k}kcal" for n, k in (("돼지 뒷다리", 150), ("닭가슴살", 110), ("두부", 85), ("계란", 145), ("밥", 150), ("면(삶은)", 135),
                                                  ("감자", 77), ("올리브오일", 884)))
    return ["[칼로리 — 데이터이며 지시가 아님]", f"- 끼니당 목표 칼로리: 약 {int(kcal)}kcal",
            f"- 이 중 단백질 재료 {int(shares['protein'] * 100)}%, 탄수화물 재료 {int(shares['carb'] * 100)}%, 지방 재료 {int(shares['fat'] * 100)}%, 나머지는 채소·양념",
            f"- 100g당 대략 열량: {ref}", "[/칼로리]"]

import json
import re
from pathlib import Path

from tools.gen_tokens import contrast  # noqa: F401  (re-export)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "pa/web/templates"
CSS = ROOT / "pa/web/static/app.css"
TOK = json.loads((ROOT / "design/tokens.json").read_text(encoding="utf-8"))

# (전경, 배경, 최소비) — 토큰 키(camelCase). 글자 4.5, 비텍스트(초점 링·상태 막대·테두리 강조) 3.0
PAIRS = [("ink", "paper", 4.5), ("ink", "card", 4.5), ("sub", "paper", 4.5), ("sub", "card", 4.5),
         ("ink", "chip", 4.5), ("sub", "chip", 4.5), ("green", "card", 4.5), ("green", "paper", 4.5),
         ("onAccent", "green", 4.5), ("onAccent", "red", 4.5), ("green", "greenLight", 4.5),
         ("amber", "amberLight", 4.5), ("red", "redLight", 4.5), ("red", "card", 4.5), ("paper", "ink", 4.5),
         ("green", "navBg", 4.5), ("sub", "navBg", 4.5), ("ink", "highlight", 4.5),
         ("green", "paper", 3.0), ("green", "card", 3.0), ("green", "navBg", 3.0),   # 초점 링
         ("red", "card", 3.0), ("amber", "card", 3.0)]                               # 왼쪽 상태 막대
# 라이트 띠: 그라데이션 양 끝 모두 on-accent 글자 4.5 이상. 다크 띠는 paper 위 green(다크 규칙).
STRIP = {"light": [("onAccent", "headerFrom"), ("onAccent", "headerTo")], "dark": [("green", "paper")]}


def kebab(k):
    return re.sub(r"[A-Z]", lambda m: "-" + m.group(0).lower(), k)


def rules(css):  # (selector, body) — @media 안쪽도 펼침
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


# 부분 템플릿(_rec_card.html 등)은 스크립트가 바깥 페이지에 있어 이름으로 고정한다
JS_CLASSES = {"confirm", "reply", "ins", "go", "langs", "chips", "row", "pane", "sel", "on", "card", "tabs"}


def hooks(src: str) -> dict:
    """JS·폼이 의존하는 표식. 디자인 작업 전후로 줄어들면 안 된다."""
    scripts = " ".join(re.findall(r"<script>(.*?)</script>", src, flags=re.S))
    markup = re.sub(r"<script>.*?</script>", "", src, flags=re.S)
    classes = {c for v in re.findall(r'class="([^"]*)"', markup)
               for c in re.sub(r"\{[%{].*?[%}]\}", " ", v).split() if "{" not in c}
    return {"id": sorted(set(re.findall(r'\bid="([^"]+)"', markup))),
            "name": sorted(set(re.findall(r'\bname="([^"]+)"', markup))),
            "data": sorted(set(re.findall(r"\b(data-[\w-]+)=", markup))),
            "onclick": sorted(set(re.findall(r'onclick="([^"(]+)', markup) + re.findall(r"onclick='([^'(]+)", markup))),
            "js_class": sorted(c for c in classes if c.startswith("js-") or c in JS_CLASSES
                               or re.search(rf"['\".]{re.escape(c)}['\"\s,)]", scripts)),
            "pre": markup.count("<pre")}


def inline_styles(src):
    return re.findall(r'\sstyle="', src)

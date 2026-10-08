import re

from design_helpers import CSS, TEMPLATES

TOKCSS = (CSS.parent / "tokens.css").read_text(encoding="utf-8")
ALL = sorted(TEMPLATES.glob("*.html"))
SCANNED = list(ALL)


def _t(f):
    return f.read_text(encoding="utf-8")


def test_no_inline_styles_in_templates():
    bad = [f.name for f in SCANNED if re.search(r'\sstyle="', _t(f))]
    assert bad == [], bad


def test_no_style_blocks_or_hex_or_raw_colors_in_templates():
    for f in SCANNED:
        t = _t(f)
        assert "<style" not in t and not re.search(r"#[0-9a-fA-F]{3,8}\b", t), f.name


def test_every_css_var_is_defined():
    defined = set(re.findall(r"(--[\w-]+):", TOKCSS + CSS.read_text(encoding="utf-8")))
    used = set()
    for src in [CSS.read_text(encoding="utf-8")] + [_t(f) for f in ALL]:      # 루틴 화면도 포함: 정의 안 된 변수는 어디서도 금지
        used |= set(re.findall(r"var\((--[\w-]+)", src))
    assert used - defined == set(), used - defined          # coach-r6 의 --danger 회귀를 잡는다


def test_no_bare_primary_class():
    assert [f.name for f in ALL if re.search(r'class="(?:[^"]* )?primary(?: [^"]*)?"', _t(f))] == []


def test_no_outline_suppression_or_positive_tabindex():
    for f in ALL:
        assert not re.search(r'tabindex="[1-9]', _t(f)), f.name
    assert "outline:none" not in CSS.read_text(encoding="utf-8").replace(" ", "")


def test_icon_only_buttons_have_accessible_name():
    for f in ALL:
        for m in re.finditer(r"<button([^>]*)>([^<]*)</button>", _t(f)):
            attrs, text = m.groups()
            if not re.search(r"[0-9A-Za-z가-힣{]", text):          # 글자 없는 버튼(이모지만/빈 버튼)
                assert "aria-label" in attrs, (f.name, m.group(0)[:80])


def test_no_autoplay_anywhere():                              # Stage 7 V4 와 같은 규칙을 미리 고정
    assert [f.name for f in ALL if "autoplay" in _t(f)] == []

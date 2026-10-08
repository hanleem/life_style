import re

import pytest
from design_helpers import CSS, PAIRS, STRIP, TEMPLATES, TOK, contrast, kebab, rules
from conftest import owner_client

css = CSS.read_text(encoding="utf-8")
TOKCSS = (CSS.parent / "tokens.css").read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_registered_pairs_meet_contrast(mode):
    c = TOK["color"][mode]
    for fg, bg, need in PAIRS:
        assert contrast(c[fg][:7], c[bg][:7]) >= need, (mode, fg, bg)
    for fg, bg in STRIP[mode]:
        assert contrast(c[fg][:7], c[bg][:7]) >= 4.5, (mode, fg, bg)


def test_every_color_background_pair_in_css_is_registered():
    names = {kebab(k): k for k in TOK["color"]["light"]}
    reg = {(f, b) for f, b, _ in PAIRS}
    for sel, body in rules(css):
        fg = re.search(r"(?<![-\w])color:\s*var\(--([\w-]+)\)", body)
        bg = re.search(r"background(?:-color)?:\s*var\(--([\w-]+)\)", body)
        if fg and bg:
            assert (names[fg.group(1)], names[bg.group(1)]) in reg, sel.strip()


def test_no_px_outside_layout_block():
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    layout = re.search(r":root\{([^}]*)\}", body).group(1)
    assert "--layout-page-max" in layout
    rest = body.replace(layout, "")
    rest = re.sub(r"@media[^{]*\{", "{", rest)          # 미디어 조건의 px 허용
    bad = [m for m in re.findall(r"(?<![\w.-])(\d+(?:\.\d+)?)px", rest) if m not in ("1", "2")]
    assert bad == [], bad


def test_layout_constants_do_not_shadow_tokens_and_have_no_color():
    layout = re.search(r":root\{([^}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S)).group(1)
    for name in re.findall(r"(--[\w-]+):", layout):
        assert name.startswith("--layout-") and f"{name}:" not in TOKCSS, name
    assert "var(--" not in layout and not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb|hsl", layout)


def test_focus_visible_defined_and_no_outline_none():
    assert re.search(r":focus-visible\s*\{[^}]*outline:\s*2px solid var\(--green\)", css)
    assert "outline:none" not in css.replace(" ", "") and "outline:0" not in css.replace(" ", "")


def test_hidden_attribute_is_never_forced():
    assert not re.search(r"\[hidden\][^{]*\{[^}]*!important", css)


@pytest.mark.parametrize("cls", ["chips", "pane", "reply", "langs"])
def test_toggled_classes_never_get_display(cls):
    for sel, body in rules(css):
        for s in sel.split(","):
            if re.search(rf"\.{cls}(?![\w-])", s) and "display" in body:
                assert ":not([hidden])" in s, s.strip()


def test_vocabulary_classes_exist():
    for cls in (".pastrip-right", ".chip-g", ".chip-a", ".chip-r", ".chip-row", ".sec-t", ".must", ".actions",
                ".btn-block", ".field", ".field-full", ".input-num", ".check-row", ".is-current", ".notice-danger",
                ".empty", ".draft-body", ".filters", ".row", ".tabs", ".text-danger", ".chip", ".btn-primary"):
        assert re.search(re.escape(cls) + r"(?![\w-])", css), cls      # Stage 7 V2도 .btn .btn-primary .meta .chip .text-danger 에 의존
    assert re.search(r"\.check-row\s*\{[^}]*min-height:\s*var\(--touch\)", css)


def test_base_header_landmark(con, tmp_path):
    from pa.settings import Settings
    from pa.web.app import create_app
    src = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'role="banner"' not in src and '<header class="pastrip">' in src
    assert "{% block strip_right %}" in src and 'id="main"' in src
    html = owner_client(create_app(Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path), con, None)).get("/approvals").text
    assert html.count("<header") == 1 and html.count("<main") == 1 and 'class="card empty"' in html

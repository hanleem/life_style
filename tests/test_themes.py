import json
from pathlib import Path

import pytest

from pa.core import profile
from tools.gen_tokens import contrast, render_css, resolved_colors

ROOT = Path(__file__).resolve().parents[1]
TOK = json.loads((ROOT / "design/tokens.json").read_text(encoding="utf-8"))


def test_theme_names_match_the_profile_choices():
    assert set(TOK["themes"]) == set(profile.THEMES) == {"pastel", "wood", "mono", "fresh"}


@pytest.mark.parametrize("theme", sorted(profile.THEMES))
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_every_theme_is_readable(theme, mode):
    c = resolved_colors(TOK, theme, mode)
    assert set(c) == set(TOK["color"]["light"])
    assert contrast(c["ink"], c["paper"]) >= 4.5
    assert contrast(c["ink"], c["card"]) >= 4.5
    assert contrast(c["sub"], c["paper"]) >= 4.5
    assert contrast(c["sub"], c["card"]) >= 4.5
    assert contrast(c["onAccent"], c["green"]) >= 4.5     # 주 버튼 글씨
    assert contrast(c["green"], c["card"]) >= 4.5         # 링크·강조 글씨
    assert contrast(c["ink"], c["chip"]) >= 4.5


def test_fresh_is_the_default_palette():
    assert resolved_colors(TOK, "fresh", "light") == TOK["color"]["light"]


def test_css_has_a_block_per_theme_and_mode():
    css = render_css(TOK)
    for t in ("pastel", "wood", "mono"):
        assert f"[data-theme={t}]{{" in css
        assert f"@media (prefers-color-scheme:dark){{[data-theme={t}]{{" in css
    assert css.index("[data-theme=wood]{") > css.index(":root{")      # 뒤에 와야 이긴다


def test_large_font_is_18px_body():
    css = render_css(TOK)
    assert "[data-font=large]{" in css and "--font-body:18px" in css.split("[data-font=large]{")[1].split("}")[0]


def test_generated_css_in_sync():
    assert (ROOT / "pa/web/static/tokens.css").read_text(encoding="utf-8") == render_css(TOK)

import json
from pathlib import Path

from tools.gen_tokens import contrast, render_css

ROOT = Path(__file__).resolve().parents[1]
TOK = json.loads((ROOT / "design/tokens.json").read_text(encoding="utf-8"))


def test_contrast_reference_values():
    assert round(contrast("#000000", "#FFFFFF"), 1) == 21.0
    assert round(contrast("#777777", "#FFFFFF"), 2) == 4.48


def test_body_text_meets_aa_both_modes():
    for mode in ("light", "dark"):
        c = TOK["color"][mode]
        assert contrast(c["ink"], c["paper"]) >= 4.5, mode
        assert contrast(c["ink"], c["card"]) >= 4.5, mode
        assert contrast(c["sub"], c["paper"]) >= 4.5, mode


def test_dark_has_every_light_key():
    assert set(TOK["color"]["dark"]) == set(TOK["color"]["light"])


def test_generated_files_in_sync():
    assert (ROOT / "pa/web/static/tokens.css").read_text(encoding="utf-8") == render_css(TOK)


def test_css_names():
    css = render_css(TOK)
    assert "--green-light:" in css and "prefers-color-scheme:dark" in css and "--touch:44px" in css
    assert "--on-accent:" in css and "--radius-card:16px" in css and "--space-md:12px" in css

from pathlib import Path
from pa.scout.config import load_scout_config


def test_defaults_when_missing(tmp_path):
    c = load_scout_config(tmp_path / "nope.yaml")
    assert c.outing.enabled is False
    assert c.taste_seeds == ()


def test_repo_default_file():
    c = load_scout_config(Path("config/scout.yaml"))
    assert c.outing.enabled is False and c.outing.from_region is None   # 출발 지역은 첫 인터뷰에서 채운다
    assert (c.outing.best_oneway_min, c.outing.max_oneway_min, c.outing.extended_oneway_min) == ((30, 60), 60, 120)


def test_outing_enabled_with_region(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("outing: {from_region: '서울 관악구', max_oneway_min: 90}\nunknown_key: 1\n", encoding="utf-8")
    c = load_scout_config(p)
    assert c.outing.enabled and c.outing.from_region == "서울 관악구" and c.outing.max_oneway_min == 90

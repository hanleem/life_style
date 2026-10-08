"""Regression: scout code must work against main's real `preference(key, value, updated_at)` schema."""
import json
from datetime import datetime, timezone

import httpx
from pathlib import Path

from pa.core import taste
from pa.scout.config import OVERRIDES_KEY, OutingCfg, ScoutConfig, effective_config
from pa.scout.outing import _apply_taste_seeds
from pa.settings import Settings
from pa.web.app import create_app

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def _cols(con):
    return [r[1] for r in con.execute("PRAGMA table_info(preference)")]


def test_create_app_keeps_main_preference_schema(con):
    before = con.execute("SELECT sql FROM sqlite_master WHERE name='preference'").fetchone()[0]
    s = Settings(dev_skip_tailscale=True, session_secret="x" * 40, scout_config_path=Path("config/scout.yaml"))
    create_app(s, con, None, http=httpx.AsyncClient())
    assert con.execute("SELECT sql FROM sqlite_master WHERE name='preference'").fetchone()[0] == before
    assert _cols(con) == ["key", "value", "updated_at"]


def test_taste_pin_on_main_schema(con):
    from pa.core import recs
    recs.ensure_schema(con)
    taste.pin_weight(con, "food", "light_menu", "yes", True, NOW)
    assert ["food", "light_menu", "yes"] in json.loads(
        con.execute("SELECT value FROM preference WHERE key='taste_pins'").fetchone()["value"])
    taste.pin_weight(con, "food", "light_menu", "yes", False, NOW)


def test_effective_config_with_saved_override(con):
    con.execute("INSERT INTO preference (key, value, updated_at) VALUES (?,?,?)",
                (OVERRIDES_KEY, json.dumps({"from_region": "분당"}), "x"))
    base = ScoutConfig(outing=OutingCfg(from_region="서울"))
    assert effective_config(con, base).outing.from_region == "분당"


def test_outing_taste_seed_on_main_schema(con):
    cfg = ScoutConfig(outing=OutingCfg(from_region="서울"),
                      taste_seeds=({"domain": "food", "feature": "light_menu", "value": "yes"},))
    _apply_taste_seeds(con, cfg, NOW)
    pins = json.loads(con.execute("SELECT value FROM preference WHERE key='taste_pins'").fetchone()["value"])
    assert ["food", "light_menu", "yes"] in pins

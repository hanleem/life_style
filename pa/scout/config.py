from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class OutingCfg:
    from_region: str | None = None
    best_oneway_min: tuple[int, int] = (30, 60)
    max_oneway_min: int = 60
    extended_oneway_min: int = 120
    extend_low_days: int = 3
    extend_rest_streak: int = 3
    budget: int | None = None
    companions: str | None = None
    official_domains: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.from_region and str(self.from_region).strip())


@dataclass(frozen=True)
class ScoutConfig:
    outing: OutingCfg = field(default_factory=OutingCfg)
    taste_seeds: tuple = ()


_BAD_WORDS = ("아파트", "마을", "빌라", "호", "층", "번지")


def region_is_acceptable(s) -> bool:
    """Dong-level region only: no building/unit words, no digits except in 'N가' / 'N동'."""
    if not isinstance(s, str) or not s.strip():
        return False
    if any(w in s for w in _BAD_WORDS):
        return False
    return not re.search(r"\d", re.sub(r"\d+(?=[가동])", "", s))


def _build(cls, data: Any):
    if not isinstance(data, dict):
        return cls()
    names = {f.name for f in fields(cls)}
    kw = {k: (tuple(v) if isinstance(v, list) else v) for k, v in data.items() if k in names}
    return cls(**kw)


def load_scout_config(path: Path) -> ScoutConfig:
    p = Path(path)
    if not p.exists():
        return ScoutConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return ScoutConfig()
    outing = _build(OutingCfg, raw.get("outing"))
    if outing.from_region is not None and not region_is_acceptable(outing.from_region):
        outing = replace(outing, from_region=None)
    return ScoutConfig(
        outing=outing,
        taste_seeds=tuple(x for x in (raw.get("taste_seeds") or ()) if isinstance(x, dict)),
    )


OVERRIDES_KEY = "scout_overrides"


def effective_config(con, base: ScoutConfig) -> ScoutConfig:
    """Overlay preference `scout_overrides` (web settings) on the file config."""
    row = con.execute("SELECT value FROM preference WHERE key=?", (OVERRIDES_KEY,)).fetchone()
    if row is None:
        return base
    try:
        ov = json.loads(row["value"])
    except ValueError:
        return base
    if not isinstance(ov, dict):
        return base
    o = {}
    if isinstance(ov.get("from_region"), str):
        r = ov["from_region"].strip()
        o["from_region"] = r if region_is_acceptable(r) else None
    b = ov.get("best_oneway_min")
    if isinstance(b, list) and len(b) == 2 and all(isinstance(x, int) for x in b):
        o["best_oneway_min"] = (b[0], b[1])
    if isinstance(ov.get("max_oneway_min"), int):
        o["max_oneway_min"] = ov["max_oneway_min"]
    return replace(base, outing=replace(base.outing, **o))

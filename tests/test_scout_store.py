from pa.scout import features as F
from pa.scout import store


def test_oneway_bucket_uses_upper_bound_half():
    assert F.oneway_bucket([90, 120]) == "30-60"   # 60
    assert F.oneway_bucket([50, 60]) == "≤30"      # 30
    assert F.oneway_bucket([50, 62]) == "30-60"    # 31
    assert F.oneway_bucket([100, 124]) == "60-90"
    assert F.oneway_bucket([150, 180]) == "60-90"  # 90
    assert F.oneway_bucket([150, 181]) == "90-120"
    assert F.oneway_bucket([200, 240]) == "90-120"
    assert F.oneway_bucket([200, 250]) == "120+"
    assert F.oneway_bucket(None) == "unknown"
    assert F.oneway_bucket([]) == "unknown"


def test_price_bucket():
    assert F.price_bucket(15000) == "≤15k"
    assert F.price_bucket(15001) == "15-30k"
    assert F.price_bucket(30000) == "15-30k"
    assert F.price_bucket(30001) == "30k+"
    assert F.price_bucket(None) == "unknown"


def test_ensure_schema_idempotent_and_columns(con):
    store.ensure_schema(con)
    store.ensure_schema(con)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(outing_suggestion)")}
    assert {"id", "for_week", "course_json", "sources_json", "status", "feedback", "created_at"} <= cols
    for t in ("content_item", "grant_notice", "grant_profile"):   # 제거된 기능의 표는 만들지 않는다
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone() is None


COURSE = {
    "drive": {"from_region": "서울", "stops": ["양평"], "roundtrip_min_est": [90, 120]},
    "walk": {"name": "숲길", "minutes": 45, "type": "숲"},
    "crowd_level": "low",
    "food": {"place": "식당A", "cuisine": "이탈리안", "price": {"value": "12,000원"},
             "takeout": "yes", "light_menu": "yes"},
}


def test_outing_and_food_features():
    o = F.outing_features(COURSE)
    assert set(o) == {"region", "oneway_bucket", "walk_type", "walk_minutes_bucket", "crowd_level"}
    assert o["oneway_bucket"] == "30-60" and o["region"] == "양평"
    assert o["crowd_level"] == "low" and o["walk_type"] == "숲"
    fd = F.food_features(COURSE)
    assert fd["place"] == "식당A" and fd["cuisine"] == "이탈리안"
    assert fd["price_bucket"] == "≤15k" and fd["light_menu"] == "yes"
    empty = F.food_features({})
    assert empty["light_menu"] == "unknown" and empty["price_bucket"] == "unknown"
    assert all(v == "unknown" for v in F.outing_features({}).values())


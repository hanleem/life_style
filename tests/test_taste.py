from datetime import datetime, timedelta, timezone
import pytest
from pa.core import recs, taste
from pa.core.taste import Candidate

T0 = datetime(2026, 10, 6, tzinfo=timezone.utc)


@pytest.fixture
def rcon(con):
    recs.ensure_schema(con)
    return con


def _rec(con, creator, now=T0, domain="food"):
    return recs.record_recommendation(con, domain=domain, item_ref=creator,
                                      features={"creator": creator, "crowd_level": "high"}, query_used=None, now=now)


def _w(con, feature, value, domain="food"):
    r = con.execute("SELECT weight, n FROM taste_weight WHERE domain=? AND feature=? AND value=?",
                    (domain, feature, value)).fetchone()
    return (round(r["weight"], 6), r["n"]) if r else None


def test_like_three_times_ranks_first(rcon):
    for _ in range(3):
        taste.apply_feedback(rcon, _rec(rcon, "A"), "like", [], None, T0)
    cands = [Candidate("x", {"creator": "B"}, {}), Candidate("y", {"creator": "A"}, {}), Candidate("z", {"creator": "C"}, {})]
    ranked = taste.rank_with_exploration(rcon, "food", cands, T0, k=3)
    assert ranked[0][0].key == "y" and ranked[0][1] is False


def test_dislike_twice_excluded_90_days(rcon):
    for _ in range(2):
        taste.apply_feedback(rcon, _rec(rcon, "Bad"), "dislike", [], None, T0)
    cands = [Candidate("b", {"creator": "Bad"}, {}), Candidate("g", {"creator": "Good"}, {})]
    assert [c.key for c, _ in taste.rank_with_exploration(rcon, "food", cands, T0 + timedelta(days=30))] == ["g"]
    assert "b" in [c.key for c, _ in taste.rank_with_exploration(rcon, "food", cands, T0 + timedelta(days=91))]


def test_one_of_three_is_exploration(rcon):
    taste.apply_feedback(rcon, _rec(rcon, "A"), "like", [], None, T0)
    taste.apply_feedback(rcon, _rec(rcon, "B"), "like", [], None, T0)
    cands = [Candidate(k, {"creator": k}, {}) for k in ["A", "B", "C", "D"]]
    ranked = taste.rank_with_exploration(rcon, "food", cands, T0, k=3)
    assert len(ranked) == 3 and sum(1 for _, e in ranked if e) == 1
    assert {c.key for c, e in ranked if not e} == {"A", "B"}


def test_open_only_does_not_change_weight(rcon):
    rid = _rec(rcon, "A")
    recs.mark_opened(rcon, rid, T0)
    assert _w(rcon, "creator", "A") is None


def test_overwrite_not_double_counted(rcon):
    rid = _rec(rcon, "A")
    taste.apply_feedback(rcon, rid, "like", [], None, T0)
    taste.apply_feedback(rcon, rid, "dislike", ["붐빔"], None, T0)
    assert _w(rcon, "creator", "A") == (-1.0, 1)
    assert _w(rcon, "crowd_level", "high") == (-1.5, 1)
    taste.apply_feedback(rcon, rid, "like", [], None, T0)
    assert _w(rcon, "creator", "A") == (1.0, 1)
    assert _w(rcon, "crowd_level", "high") == (1.0, 1)


def test_decay_on_write(rcon):
    taste.apply_feedback(rcon, _rec(rcon, "A"), "like", [], None, T0)
    taste.apply_feedback(rcon, _rec(rcon, "A", T0 + timedelta(weeks=2)), "like", [], None, T0 + timedelta(weeks=2))
    assert _w(rcon, "creator", "A") == (round(1.0 * 0.97 ** 2 + 1.0, 6), 2)


def test_profile_text(rcon):
    assert taste.taste_profile_text(rcon, "food") == "아직 학습된 취향 없음"
    taste.apply_feedback(rcon, _rec(rcon, "A"), "like", [], None, T0)
    assert "선호:" in taste.taste_profile_text(rcon, "food") and "A" in taste.taste_profile_text(rcon, "food")


def test_pin_floor_delete_reset_list(rcon):
    taste.apply_feedback(rcon, _rec(rcon, "A"), "soso", [], None, T0)
    cands = [Candidate("a", {"creator": "A"}, {}), Candidate("b", {"creator": "B"}, {})]
    base = taste.score_candidate(rcon, "food", {"creator": "A"}, T0)
    assert round(base, 6) == round(0.1 / 2 ** 0.5, 6)
    taste.pin_weight(rcon, "food", "creator", "A")
    assert round(taste.score_candidate(rcon, "food", {"creator": "A"}, T0), 6) == round(1.0 / 2 ** 0.5, 6)
    assert "📌" in taste.taste_profile_text(rcon, "food")
    taste.apply_feedback(rcon, _rec(rcon, "N"), "dislike", [], None, T0)
    taste.pin_weight(rcon, "food", "creator", "N")
    taste.apply_feedback(rcon, _rec(rcon, "Z"), "dislike", [], None, T0)
    rows = taste.list_weights(rcon, "food")
    assert {(r["feature"], r["value"]) for r in rows} >= {("creator", "A"), ("creator", "N")}
    # negative pinned stays negative, magnitude >= 1
    assert taste.score_candidate(rcon, "food", {"creator": "N"}, T0) < 0
    taste.pin_weight(rcon, "food", "creator", "A", pinned=False)
    assert round(taste.score_candidate(rcon, "food", {"creator": "A"}, T0), 6) == round(base, 6)
    taste.delete_weight(rcon, "food", "creator", "A")
    assert _w(rcon, "creator", "A") is None
    taste.apply_feedback(rcon, _rec(rcon, "Q", domain="outing"), "like", [], None, T0)
    taste.reset(rcon, "food")
    assert taste.list_weights(rcon, "food") == []
    assert len(taste.list_weights(rcon)) >= 1
    taste.reset(rcon)
    assert taste.list_weights(rcon) == []


def test_unknown_reason_feature_no_penalty(rcon):
    rid = _rec(rcon, "A")
    taste.apply_feedback(rcon, rid, "dislike", ["없는이유", "bogus"], None, T0)
    assert _w(rcon, "creator", "A") == (-1.0, 1)
    assert _w(rcon, "crowd_level", "high") == (-1.0, 1)

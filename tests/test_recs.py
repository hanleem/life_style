from datetime import datetime, timezone
import pytest
from pa.core import recs
from pa.core.errors import NotFound

NOW = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def rcon(con):
    recs.ensure_schema(con)
    recs.ensure_schema(con)  # 멱등
    return con


def test_record_and_open(rcon):
    rid = recs.record_recommendation(rcon, domain="food", item_ref="c1",
                                     features={"creator": "A", "lang": "ko"}, query_used="q", now=NOW)
    recs.mark_opened(rcon, rid, NOW)
    recs.mark_opened(rcon, rid, datetime(2026, 10, 7, tzinfo=timezone.utc))
    row = recs.get_recommendation(rcon, rid)
    assert row["opened_at"] == NOW.isoformat()
    assert rcon.execute("SELECT COUNT(*) FROM taste_weight").fetchone()[0] == 0


def test_feedback_history_kept_last_wins(rcon):
    rid = recs.record_recommendation(rcon, domain="food", item_ref="c2", features={}, query_used=None, now=NOW)
    recs.insert_feedback(rcon, rid, "like", [], None, NOW)
    recs.insert_feedback(rcon, rid, "dislike", ["붐빔"], None, datetime(2026, 10, 6, 1, tzinfo=timezone.utc))
    assert recs.last_rating(rcon, rid) == ("dislike", ["붐빔"])
    assert rcon.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 2


def test_reasons_filtered(rcon):
    assert recs.validate_reasons("outing", ["붐빔", "해킹", "붐빔"]) == ["붐빔"]
    assert recs.validate_reasons("unknown", ["붐빔"]) == []


def test_missing_rec(rcon):
    with pytest.raises(NotFound):
        recs.get_recommendation(rcon, "nope")

from datetime import datetime, timedelta, timezone

import pytest

from pa.core import routine_plan as rp
from pa.core import supply
from pa.core.errors import NotFound

T = datetime(2026, 10, 6, 8, 0, tzinfo=timezone(timedelta(hours=9)))


@pytest.fixture
def key(con):
    iid = supply.add_item(con, label="샐러드 정기배송", meals_per_workday=2, lead_days=1, now=T)
    return f"supply_lead:{iid}"


def _lead(con, key):
    return supply.get_item(con, key.removeprefix("supply_lead:")).lead_days


def test_no_user_setting_keys_remain(con):
    assert rp.CORE_KEYS == {}
    for k in ("walk_minutes", "evening_checkin_at"):
        with pytest.raises(ValueError):
            rp.get_value(con, k)
        with pytest.raises(ValueError):
            rp.set_value(con, k, 30, source="user", now=T)


def test_set_value_rejects_unknown_key_and_source(con):
    with pytest.raises(ValueError):
        rp.set_value(con, "anything", 30, source="user", now=T)
    with pytest.raises(ValueError):
        rp.set_value(con, "anything", 30, source="coach", now=T)


def test_reexpose_14_days(con, key):
    assert rp.propose(con, key, 3, basis="b", now=T)
    assert rp.propose(con, key, 3, basis="b", now=T + timedelta(days=13)) is None
    assert rp.propose(con, key, 3, basis="b", now=T + timedelta(days=15))
    assert rp.propose(con, key, 4, basis="b", now=T + timedelta(days=1))  # other value ok


def test_passed_keeps_value_and_still_blocks_reexpose(con, key):
    p = rp.propose(con, key, 3, basis="b", now=T)
    rp.respond(con, p["id"], "passed", now=T)
    assert _lead(con, key) == 1
    assert rp.propose(con, key, 3, basis="b", now=T + timedelta(days=2)) is None
    assert rp.open_proposals(con, now=T) == []


def test_accept_and_edit_update_the_supply_lead_days(con, key):
    p = rp.propose(con, key, 3, basis="b", now=T)
    assert rp.open_proposals(con, now=T)[0]["id"] == p["id"]
    rp.respond(con, p["id"], "accepted", now=T)
    assert _lead(con, key) == 3
    q = rp.propose(con, key, 5, basis="b", now=T)
    rp.respond(con, q["id"], "edited", value=4, now=T)
    assert _lead(con, key) == 4


def test_double_respond_and_unknown(con, key):
    p = rp.propose(con, key, 3, basis="b", now=T)
    rp.respond(con, p["id"], "passed", now=T)
    with pytest.raises(ValueError):
        rp.respond(con, p["id"], "accepted", now=T)
    with pytest.raises(NotFound):
        rp.respond(con, "nope", "accepted", now=T)


def test_open_proposals_expire_after_14_days(con, key):
    rp.propose(con, key, 3, basis="b", now=T)
    assert len(rp.open_proposals(con, now=T + timedelta(days=13))) == 1
    assert rp.open_proposals(con, now=T + timedelta(days=15)) == []


def test_invalid_edit_does_not_consume_proposal(con, key):
    p = rp.propose(con, key, 3, basis="b", now=T)
    with pytest.raises(ValueError):
        rp.respond(con, p["id"], "edited", value=500, now=T)
    rp.respond(con, p["id"], "accepted", now=T)
    assert _lead(con, key) == 3


def test_unknown_key_cannot_be_proposed(con):
    with pytest.raises(ValueError):
        rp.propose(con, "walk_minutes", 25, basis="x", now=T)


def test_supply_lead_proposal_key(con):
    iid = supply.add_item(con, label="샐러드 정기배송", meals_per_workday=2, now=T)
    assert rp.propose(con, f"supply_lead:{iid}", 3, basis="b", now=T)
    with pytest.raises(NotFound):
        rp.propose(con, "supply_lead:missing", 3, basis="b", now=T)
    with pytest.raises(ValueError):
        rp.propose(con, f"supply_lead:{iid}", 99, basis="b", now=T)

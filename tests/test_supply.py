import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from pa.core import coach_copy, routine_plan, supply
from pa.core.errors import NotFound

KST = timezone(timedelta(hours=9))


def at(y, m, d, h=8, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=KST)


MON, TUE, WED, THU, FRI, SAT, SUN = (date(2026, 10, 5 + i) for i in range(7))
NOW0 = at(2026, 10, 5)


def mk(con, **kw):
    kw.setdefault("label", "샐러드 정기배송")
    kw.setdefault("meals_per_workday", 2)
    kw.setdefault("now", NOW0)
    return supply.add_item(con, **kw)


def rem(con, iid):
    return supply.get_item(con, iid).remaining


def test_reorder_point_uniform_is_d_times_l_plus_s(con):
    i = supply.get_item(con, mk(con, meals_per_weekend_day=2))
    assert supply.reorder_point(i, TUE) == 4
    i2 = supply.get_item(con, mk(con, meals_per_weekend_day=2, lead_days=2))
    assert supply.reorder_point(i2, TUE) == 6
    i3 = supply.get_item(con, mk(con, meals_per_weekend_day=2, lead_days=0, safety_days=0))
    assert supply.reorder_point(i3, TUE) == 0


def test_reminder_boundary(con):
    iid = mk(con, remaining=5, meals_per_weekend_day=2)
    assert supply.due_reminders(con, TUE) == []
    supply.adjust(con, iid, 4, now=at(2026, 10, 6))
    assert [x.id for x in supply.due_reminders(con, TUE)] == [iid]
    supply.adjust(con, iid, 3, now=at(2026, 10, 6))
    assert [x.id for x in supply.due_reminders(con, TUE)] == [iid]


def test_weekend_zero_skips_weekend_in_window(con):
    i = supply.get_item(con, mk(con))
    assert supply.reorder_point(i, FRI) == 4  # Mon + Tue
    assert supply.reorder_point(i, THU) == 4  # Fri + Mon
    assert supply.reorder_point(i, SAT) == 4


def test_weekend_zero_friday_not_zero_boundary(con):
    iid = mk(con, remaining=4)
    assert [x.id for x in supply.due_reminders(con, FRI)] == [iid]
    supply.adjust(con, iid, 5, now=at(2026, 10, 9))
    assert supply.due_reminders(con, FRI) == []


def test_weekend_meals_counted(con):
    i = supply.get_item(con, mk(con, meals_per_weekend_day=1))
    assert supply.reorder_point(i, FRI) == 2


def test_weekend_d_sum_over_mixed_days(con):
    i = supply.get_item(con, mk(con, meals_per_weekend_day=1, lead_days=2, safety_days=1))
    assert supply.reorder_point(i, THU) == 2 + 1 + 1  # Fri, Sat, Sun


def test_consume_workday_weekend_and_holiday(con):
    iid = mk(con, remaining=20)
    supply.consume_through(con, at(2026, 10, 9).date(), now=at(2026, 10, 9))
    assert rem(con, iid) == 18  # Hangul day (Fri) still deducts
    supply.consume_through(con, SAT, now=at(2026, 10, 10))
    assert rem(con, iid) == 18  # weekend 0: nothing
    assert con.execute("SELECT COUNT(*) FROM supply_event WHERE for_date=?", (SAT.isoformat(),)).fetchone()[0] == 0
    j = mk(con, remaining=10, meals_per_weekend_day=1)
    supply.consume_through(con, SUN, now=at(2026, 10, 11))
    assert rem(con, j) == 9


def test_consume_is_idempotent_and_catches_up(con):
    iid = mk(con, remaining=20)
    supply.consume_through(con, MON, now=NOW0)
    assert supply.consume_through(con, MON, now=NOW0) == 0
    assert rem(con, iid) == 18
    assert supply.consume_through(con, THU, now=at(2026, 10, 8)) == 3
    assert rem(con, iid) == 12


def test_consume_idempotent_even_without_cursor(con):
    iid = mk(con, remaining=20)
    supply.consume_through(con, MON, now=NOW0)
    con.execute("DELETE FROM sync_cursor WHERE key='supply:consumed_through'")
    supply.consume_through(con, MON, now=NOW0)
    assert rem(con, iid) == 18


def test_partial_unique_index_blocks_duplicate_consumed(con):
    iid = mk(con, remaining=5)
    supply.consume_through(con, MON, now=NOW0)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO supply_event(id,item_id,kind,qty,for_date,created_at) "
                    "VALUES('x',?,'consumed',1,?,'t')", (iid, MON.isoformat()))


def test_catchup_limited_to_14_days_and_not_before_item_existed(con):
    iid = mk(con, remaining=500, now=at(2026, 10, 5))
    con.execute("INSERT INTO sync_cursor(key,value,updated_at) VALUES('supply:consumed_through','2026-08-01','t')")
    supply.consume_through(con, date(2026, 10, 12), now=at(2026, 10, 12))
    # item created 10-05: Mon..Fri(5) + Mon(10-12) = 6 workdays * 2
    assert rem(con, iid) == 500 - 12


def test_floor_at_zero(con):
    iid = mk(con, remaining=1)
    supply.consume_through(con, MON, now=NOW0)
    assert rem(con, iid) == 0
    q = con.execute("SELECT qty FROM supply_event WHERE kind='consumed'").fetchone()[0]
    assert q == 1
    supply.consume_through(con, TUE, now=at(2026, 10, 6))
    assert rem(con, iid) == 0
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE supply_stock SET remaining=-1")


def test_deliver_skip_adjust(con):
    iid = mk(con, remaining=0)
    assert supply.deliver(con, iid, 10, now=NOW0)["remaining"] == 10
    supply.consume_through(con, MON, now=NOW0)
    assert rem(con, iid) == 8
    supply.skip(con, iid, MON, now=NOW0)
    assert rem(con, iid) == 10
    with pytest.raises(ValueError):
        supply.skip(con, iid, MON, now=NOW0)
    assert rem(con, iid) == 10
    with pytest.raises(ValueError):
        supply.skip(con, iid, TUE, now=NOW0)  # nothing deducted that day
    supply.adjust(con, iid, 7, now=NOW0)
    assert rem(con, iid) == 7
    assert con.execute("SELECT qty FROM supply_event WHERE kind='adjust'").fetchone()[0] == -3


def test_skip_partial_qty_and_cap(con):
    iid = mk(con, remaining=10)
    supply.consume_through(con, MON, now=NOW0)
    supply.skip(con, iid, MON, qty=1, now=NOW0)
    assert rem(con, iid) == 9


def test_skip_cap_exact(con):
    iid = mk(con, remaining=10)
    supply.consume_through(con, MON, now=NOW0)
    supply.skip(con, iid, MON, qty=99, now=NOW0)
    assert rem(con, iid) == 10


def test_skip_when_deduction_was_zero(con):
    iid = mk(con, remaining=0)
    supply.consume_through(con, MON, now=NOW0)
    with pytest.raises(ValueError):
        supply.skip(con, iid, MON, now=NOW0)
    assert rem(con, iid) == 0


def test_deliver_and_adjust_validation(con):
    iid = mk(con)
    for q in (0, 61, -1):
        with pytest.raises(ValueError):
            supply.deliver(con, iid, q, now=NOW0)
    with pytest.raises(ValueError):
        supply.adjust(con, iid, -1, now=NOW0)
    with pytest.raises(NotFound):
        supply.deliver(con, "nope", 5, now=NOW0)


def test_reminder_once_per_cycle(con):
    iid = mk(con, remaining=3)
    assert [x.id for x in supply.due_reminders(con, TUE)] == [iid]
    supply.mark_reminded(con, [iid], now=at(2026, 10, 6))
    assert supply.due_reminders(con, WED) == []
    assert supply.due_reminders(con, THU) == []
    supply.deliver(con, iid, 1, now=at(2026, 10, 8))  # 4 <= R=4 after delivery
    assert [x.id for x in supply.due_reminders(con, THU)] == [iid]  # new cycle -> reset


def test_snooze_until_next_weekday(con):
    iid = mk(con, remaining=0)
    supply.mark_reminded(con, [iid], now=at(2026, 10, 9))
    assert supply.snooze(con, iid, FRI, now=at(2026, 10, 9)) == date(2026, 10, 12)
    assert supply.due_reminders(con, FRI) == []
    assert supply.due_reminders(con, SAT) == []
    assert supply.due_reminders(con, SUN) == []
    assert [x.id for x in supply.due_reminders(con, date(2026, 10, 12))] == [iid]
    supply.mark_reminded(con, [iid], now=at(2026, 10, 12))
    assert supply.due_reminders(con, date(2026, 10, 13)) == []  # once again only once
    assert supply.snooze(con, iid, MON, now=NOW0) == TUE


def test_no_reminder_on_weekend(con):
    mk(con, remaining=0)
    assert supply.due_reminders(con, SAT) == []
    assert supply.due_reminders(con, SUN) == []


def test_remaining_above_threshold_after_delivery_never_due(con):
    iid = mk(con, remaining=0)
    supply.deliver(con, iid, 10, now=NOW0)
    assert supply.due_reminders(con, TUE) == []


def test_pause_stops_deduction_and_reminders(con):
    iid = mk(con, remaining=3)
    supply.pause(con, iid, TUE, THU)
    supply.consume_through(con, THU, now=at(2026, 10, 8))
    assert con.execute("SELECT COUNT(*) FROM supply_event WHERE kind='consumed' AND for_date>=?",
                       (TUE.isoformat(),)).fetchone()[0] == 0
    assert supply.due_reminders(con, WED) == []
    assert supply.is_paused(supply.get_item(con, iid), WED)
    supply.resume(con, iid)
    assert [x.id for x in supply.due_reminders(con, WED)] == [iid]
    with pytest.raises(ValueError):
        supply.pause(con, iid, THU, TUE)


def test_pause_makes_paused_days_zero_in_reorder_point(con):
    iid = mk(con, remaining=3, meals_per_weekend_day=2)
    supply.pause(con, iid, WED, THU)
    assert supply.reorder_point(supply.get_item(con, iid), TUE) == 2 + 2  # Fri, Sat


def test_cutoff_asked_once(con):
    iid = mk(con, order_url="https://example.com/order")
    r = supply.mark_ordered(con, iid, now=NOW0)
    assert r["ask_cutoff"] is True and r["order_url"] == "https://example.com/order"
    supply.set_cutoff(con, iid, None)
    r2 = supply.mark_ordered(con, iid, now=NOW0 + timedelta(hours=13))
    assert r2["ask_cutoff"] is False
    j = mk(con)
    supply.mark_ordered(con, j, now=NOW0)
    supply.set_cutoff(con, j, "19:00")
    assert supply.mark_ordered(con, j, now=NOW0 + timedelta(days=2))["ask_cutoff"] is False
    assert supply.get_item(con, j).order_cutoff == "19:00"
    with pytest.raises(ValueError):
        supply.set_cutoff(con, j, "7pm")


def test_cutoff_given_at_creation_not_asked(con):
    iid = mk(con, order_cutoff="18:30")
    assert supply.mark_ordered(con, iid, now=NOW0)["ask_cutoff"] is False


def test_double_tap_reuses_order(con):
    iid = mk(con)
    a = supply.mark_ordered(con, iid, now=NOW0)
    b = supply.mark_ordered(con, iid, now=NOW0 + timedelta(hours=11))
    c = supply.mark_ordered(con, iid, now=NOW0 + timedelta(hours=25))
    assert a["order_id"] == b["order_id"] != c["order_id"]


def test_cutoff_hint_only_when_known(con):
    i = supply.get_item(con, mk(con))
    assert supply.cutoff_hint(i, at(2026, 10, 6, 18)) is None
    supply.set_cutoff(con, i.id, "19:00")
    i = supply.get_item(con, i.id)
    assert supply.cutoff_hint(i, at(2026, 10, 6, 18)) == "오늘 19:00 전"
    assert supply.cutoff_hint(i, at(2026, 10, 6, 19, 0)) is None
    assert supply.cutoff_hint(i, at(2026, 10, 6, 19, 30)) is None
    # UTC-aware input is converted to KST
    assert supply.cutoff_hint(i, datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)) == "오늘 19:00 전"


def _cycle(con, iid, order_day, deliver_day):
    supply.mark_ordered(con, iid, now=at(2026, 9, order_day, 9))
    return supply.deliver(con, iid, 10, now=at(2026, 9, deliver_day, 9))


def test_lead_candidate_median_needs_three(con):
    iid = mk(con)
    _cycle(con, iid, 1, 2)
    _cycle(con, iid, 5, 8)
    assert supply.lead_candidate(con, iid) is None
    _cycle(con, iid, 10, 12)
    assert supply.lead_candidate(con, iid) == 2  # (1,3,2)


def test_lead_candidate_uses_last_three_only(con):
    iid = mk(con)
    _cycle(con, iid, 1, 10)  # 9, ignored
    _cycle(con, iid, 11, 13)
    _cycle(con, iid, 14, 16)
    _cycle(con, iid, 17, 22)
    assert supply.lead_candidate(con, iid) == 2  # (2,2,5)


def test_lead_proposal_not_applied_until_accepted(con):
    iid = mk(con)
    _cycle(con, iid, 1, 2)
    _cycle(con, iid, 5, 8)
    supply.mark_ordered(con, iid, now=at(2026, 9, 10, 9))
    out = supply.deliver(con, iid, 5, now=at(2026, 9, 12, 9))
    p = out["lead_proposal"]
    assert p and p["value"] == 2 and p["basis"] == coach_copy.LEAD_PROPOSAL.format(n=3, days=2)
    assert supply.get_item(con, iid).lead_days == 1
    routine_plan.respond(con, p["id"], "accepted", now=at(2026, 9, 12, 10))
    assert supply.get_item(con, iid).lead_days == 2


def test_lead_proposal_passed_keeps_l_and_not_reproposed(con):
    iid = mk(con)
    _cycle(con, iid, 1, 2)
    _cycle(con, iid, 5, 8)
    supply.mark_ordered(con, iid, now=at(2026, 9, 10, 9))
    p = supply.deliver(con, iid, 5, now=at(2026, 9, 12, 9))["lead_proposal"]
    routine_plan.respond(con, p["id"], "passed", now=at(2026, 9, 12, 10))
    assert supply.get_item(con, iid).lead_days == 1
    assert supply.deliver(con, iid, 5, now=at(2026, 9, 13, 9))["lead_proposal"] is None


def test_lead_proposal_edited(con):
    iid = mk(con)
    _cycle(con, iid, 1, 2)
    _cycle(con, iid, 5, 8)
    supply.mark_ordered(con, iid, now=at(2026, 9, 10, 9))
    p = supply.deliver(con, iid, 5, now=at(2026, 9, 12, 9))["lead_proposal"]
    routine_plan.respond(con, p["id"], "edited", value=3, now=at(2026, 9, 12, 10))
    assert supply.get_item(con, iid).lead_days == 3


def test_no_proposal_when_candidate_equals_current(con):
    iid = mk(con, lead_days=2)
    _cycle(con, iid, 1, 3)
    _cycle(con, iid, 5, 7)
    supply.mark_ordered(con, iid, now=at(2026, 9, 10, 9))
    assert supply.deliver(con, iid, 5, now=at(2026, 9, 12, 9))["lead_proposal"] is None


def test_deliver_closes_open_order(con):
    iid = mk(con)
    supply.mark_ordered(con, iid, now=NOW0)
    supply.deliver(con, iid, 5, now=NOW0 + timedelta(days=2))
    assert con.execute("SELECT delivered_at FROM supply_order").fetchone()[0] is not None


@pytest.mark.parametrize("bad", ["http://example.com/o", "javascript:alert(1)", "data:text/html,x",
                                 "ftp://example.com", "https://", "//example.com", "example.com"])
def test_https_only(con, bad):
    with pytest.raises(ValueError):
        supply.validate_https(bad)
    with pytest.raises(ValueError):
        mk(con, order_url=bad)
    iid = mk(con)
    with pytest.raises(ValueError):
        supply.update_item(con, iid, order_url=bad)
    with pytest.raises(ValueError):
        supply.add_rotation(con, iid, label="후보", url=bad, now=NOW0)


def test_https_ok_and_empty(con):
    assert supply.validate_https("https://example.com/order") == "https://example.com/order"
    assert supply.validate_https(None) is None and supply.validate_https("") is None
    iid = mk(con, order_url="https://example.com/order")
    supply.update_item(con, iid, order_url=None)
    assert supply.get_item(con, iid).order_url is None


def test_item_validation_and_update(con):
    for kw in (dict(meals_per_workday=0), dict(meals_per_workday=7), dict(meals_per_weekend_day=7),
               dict(lead_days=15), dict(safety_days=-1), dict(order_cutoff="25:00"), dict(label=" ")):
        with pytest.raises(ValueError):
            mk(con, **kw)
    iid = mk(con)
    supply.update_item(con, iid, meals_per_workday=3, order_cutoff="18:00")
    i = supply.get_item(con, iid)
    assert i.meals_per_workday == 3 and i.order_cutoff == "18:00" and i.cutoff_asked == 1
    with pytest.raises(ValueError):
        supply.update_item(con, iid, remaining=99)  # not an editable field
    with pytest.raises(NotFound):
        supply.get_item(con, "nope")
    supply.update_item(con, iid, active=False)
    assert supply.list_items(con) == [] and len(supply.list_items(con, active_only=False)) == 1


def test_inactive_item_not_deducted_or_reminded(con):
    iid = mk(con, remaining=0)
    supply.update_item(con, iid, active=False)
    assert supply.due_reminders(con, TUE) == []
    supply.consume_through(con, MON, now=NOW0)
    assert con.execute("SELECT COUNT(*) FROM supply_event").fetchone()[0] == 0


def test_stale_check_once(con):
    iid = mk(con, remaining=40, now=at(2026, 10, 1))
    supply.deliver(con, iid, 1, now=at(2026, 10, 1, 9))
    for d in range(5, 10):
        supply.consume_through(con, date(2026, 10, d), now=at(2026, 10, d, 8))
    assert supply.stale_check_needed(con, iid, date(2026, 10, 7)) is False  # 6 days
    assert supply.stale_check_needed(con, iid, date(2026, 10, 8)) is True   # 7 days
    supply.mark_stale_prompted(con, iid, now=at(2026, 10, 8))
    assert supply.stale_check_needed(con, iid, date(2026, 10, 9)) is False
    supply.deliver(con, iid, 5, now=at(2026, 10, 9, 9))
    assert supply.stale_check_needed(con, iid, date(2026, 10, 10)) is False  # new cycle, fresh


def test_stale_requires_consumption(con):
    iid = mk(con, remaining=40, now=at(2026, 9, 1))
    assert supply.stale_check_needed(con, iid, date(2026, 10, 1)) is False


def test_adjust_resets_stale(con):
    iid = mk(con, remaining=40, now=at(2026, 10, 1))
    for d in range(5, 9):
        supply.consume_through(con, date(2026, 10, d), now=at(2026, 10, d, 8))
    assert supply.stale_check_needed(con, iid, date(2026, 10, 8)) is True
    supply.adjust(con, iid, 20, now=at(2026, 10, 8, 9))
    assert supply.stale_check_needed(con, iid, date(2026, 10, 9)) is False


def test_rotation_first_weekday_and_candidates(con):
    iid = mk(con)
    assert supply.rotation_card(con, date(2026, 11, 1)) is None  # Sunday, before first weekday
    card = supply.rotation_card(con, date(2026, 11, 2))
    assert card == {"prompt": coach_copy.ROTATION_EMPTY, "candidates": []}
    supply.add_rotation(con, iid, label="후보 A", url="https://example.com/a", now=NOW0)
    card = supply.rotation_card(con, date(2026, 11, 2))
    assert card["prompt"] == coach_copy.ROTATION_PROMPT
    assert [c["label"] for c in card["candidates"]] == ["후보 A"]
    supply.dismiss_rotation(con, date(2026, 11, 2))
    assert supply.rotation_card(con, date(2026, 11, 20)) is None
    assert supply.rotation_card(con, date(2026, 12, 1)) is not None  # next month again


def test_rotation_dismissed_month_hidden_including_month_end(con):
    mk(con)
    supply.dismiss_rotation(con, date(2026, 10, 2))
    assert supply.rotation_card(con, date(2026, 10, 31)) is None


def test_no_order_or_payment_api():
    forbidden = ("pay", "purchase", "checkout", "place_order", "submit_order")
    for n in dir(supply):
        assert not any(f in n.lower() for f in forbidden), n


def test_user_visible_texts_come_from_coach_copy(con):
    iid = mk(con, remaining=0)
    i = supply.get_item(con, iid)
    for t in (coach_copy.STOCK_CHECK, coach_copy.REMAINING_LABEL, coach_copy.CUTOFF_QUESTION):
        assert coach_copy.violations(t) == []
    assert supply.cutoff_hint(supply.get_item(con, iid), at(2026, 10, 6)) is None


# ---- fix round 1 ----

def test_deliver_with_out_of_range_lead_median_succeeds_without_proposal(con):
    iid = mk(con, remaining=0)
    for o in (1, 2, 3):
        supply.mark_ordered(con, iid, now=at(2026, 8, o, 9))
        supply.deliver(con, iid, 5, now=at(2026, 8, o, 9) + timedelta(days=20))
    assert supply.lead_candidate(con, iid) == 20
    assert rem(con, iid) == 15  # each delivery added once
    assert con.execute("SELECT COUNT(*) FROM routine_proposal").fetchone()[0] == 0
    n_events = con.execute("SELECT COUNT(*) FROM supply_event WHERE kind='delivered'").fetchone()[0]
    assert n_events == 3


def _boom_trigger(con, table, op="UPDATE"):
    con.execute(f"CREATE TRIGGER boom BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'boom'); END")


def test_consume_through_atomic_on_stock_failure(con):
    iid = mk(con, remaining=10)
    _boom_trigger(con, "supply_stock")
    with pytest.raises(sqlite3.DatabaseError):
        supply.consume_through(con, TUE, now=at(2026, 10, 6))
    assert con.execute("SELECT COUNT(*) FROM supply_event").fetchone()[0] == 0
    assert con.in_transaction is False
    con.execute("DROP TRIGGER boom")
    supply.consume_through(con, TUE, now=at(2026, 10, 6))
    assert rem(con, iid) == 8


def test_deliver_atomic_on_failure(con):
    iid = mk(con, remaining=3)
    _boom_trigger(con, "supply_order")
    con.execute("DROP TRIGGER boom")
    supply.mark_ordered(con, iid, now=NOW0)
    _boom_trigger(con, "supply_order")
    with pytest.raises(sqlite3.DatabaseError):
        supply.deliver(con, iid, 5, now=NOW0 + timedelta(days=1))
    assert rem(con, iid) == 3
    assert con.execute("SELECT COUNT(*) FROM supply_event WHERE kind='delivered'").fetchone()[0] == 0


def test_skip_and_adjust_atomic(con):
    iid = mk(con, remaining=10)
    supply.consume_through(con, MON, now=at(2026, 10, 5, 22))
    _boom_trigger(con, "supply_stock")
    with pytest.raises(sqlite3.DatabaseError):
        supply.skip(con, iid, MON, now=at(2026, 10, 6))
    assert con.execute("SELECT COUNT(*) FROM supply_event WHERE kind='skipped'").fetchone()[0] == 0
    with pytest.raises(sqlite3.DatabaseError):
        supply.adjust(con, iid, 50, now=at(2026, 10, 6))
    assert con.execute("SELECT COUNT(*) FROM supply_event WHERE kind='adjust'").fetchone()[0] == 0
    con.execute("DROP TRIGGER boom")
    supply.skip(con, iid, MON, now=at(2026, 10, 6))
    assert rem(con, iid) == 10


def test_nested_in_caller_transaction(con):
    con.execute("BEGIN")
    iid = mk(con, remaining=4)
    supply.adjust(con, iid, 6, now=NOW0)
    assert con.in_transaction
    con.execute("ROLLBACK")
    assert con.execute("SELECT COUNT(*) FROM supply_item").fetchone()[0] == 0


def test_respond_atomic(con):
    iid = mk(con)
    p = routine_plan.propose(con, f"{routine_plan.LEAD_PREFIX}{iid}", 3, basis="b", now=NOW0)
    _boom_trigger(con, "routine_proposal")
    with pytest.raises(sqlite3.DatabaseError):
        routine_plan.respond(con, p["id"], "accepted", now=NOW0)
    assert supply.get_item(con, iid).lead_days == 1
    con.execute("DROP TRIGGER boom")
    routine_plan.respond(con, p["id"], "accepted", now=NOW0)
    assert supply.get_item(con, iid).lead_days == 3


@pytest.mark.parametrize("bad", [
    "https://user:pw@example.com/", "https://good.example.com@evil.example.com/", "https://:443", "https://@",
    "https://exa mple.com/", "https://example.com/a b", "ht\ntps://a.com", "https://a.com/\n",
    "https://a.com/\t", "https://a.com\\evil.com", "https://a.com/\x00", " https://a.com/",
    "https://аpple.com/", "https://例え.jp/", "https://a.com/ ",
])
def test_https_hardening_rejects(bad):
    with pytest.raises(ValueError):
        supply.validate_https(bad)


def test_https_scheme_lowercased():
    assert supply.validate_https("HTTPS://Example.com/Order") == "https://Example.com/Order"
    assert supply.validate_https("https://xn--e1afmkfd.xn--p1ai/x?q=1#f") == "https://xn--e1afmkfd.xn--p1ai/x?q=1#f"


def test_reorder_point_long_horizon_uniform(con):
    i = supply.get_item(con, mk(con, meals_per_workday=2, meals_per_weekend_day=2,
                                lead_days=14, safety_days=14))
    assert supply.reorder_point(i, TUE) == 56


def test_minor_fixes(con):
    iid = mk(con)
    supply.update_item(con, iid, label="  새 이름  ")
    assert supply.get_item(con, iid).label == "새 이름"
    naive = datetime(2026, 10, 5, 8)
    with pytest.raises(ValueError):
        mk(con, now=naive)
    with pytest.raises(ValueError):
        supply.adjust(con, iid, 3, now=naive)
    # rotation ignores inactive items
    supply.add_rotation(con, iid, label="후보", url="https://example.com/a", now=NOW0)
    assert len(supply.rotation_card(con, date(2026, 11, 2))["candidates"]) == 1
    supply.update_item(con, iid, active=0)
    assert supply.rotation_card(con, date(2026, 11, 2))["candidates"] == []
    # injectable now
    supply.dismiss_rotation(con, date(2026, 11, 2), now=at(2026, 11, 2))
    assert con.execute("SELECT updated_at FROM preference WHERE key='routine.rotation_dismissed'"
                       ).fetchone()[0].startswith("2026-11-01T23:00")

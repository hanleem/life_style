from datetime import date

from pa.core.todos import suggest_carryover


def test_suggest_carryover():
    todos = [{"id": 1, "text": "a", "date": "2026-10-02", "done": 0}, {"id": 2, "text": "b", "date": "2026-10-06", "done": 0},
             {"id": 3, "text": "c", "date": "2026-10-01", "done": 1}]
    out = suggest_carryover(todos, date(2026, 10, 6))
    assert [(t["todo_id"], t["date"]) for t in out] == [(1, "2026-10-07")]


def test_suggest_carryover_skips_weekend():
    todos = [{"id": 1, "text": "a", "date": "2026-10-08", "done": 0}]
    assert suggest_carryover(todos, date(2026, 10, 9))[0]["date"] == "2026-10-12"   # Fri -> Mon

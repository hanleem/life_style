"""할 일: 추가·완료·지우기(묘비)·되돌리기·중복 제안·병합 (오프라인, 가짜 시계)."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pa.core import task_ops
from pa.core.errors import IllegalTransition, NotFound

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=KST)


def mk(con, tid, title, *, status="open", due=None, created=None, src=None):
    c = (created or NOW).astimezone(timezone.utc).isoformat()
    con.execute("INSERT INTO task(id,title,bucket,due_at,due_confidence,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (tid, title, "check_soon", due.astimezone(timezone.utc).isoformat() if due else None,
                 "explicit" if due else "unknown", status, c, c))
    if src:
        con.execute("INSERT INTO task_source(id,task_id,source_type,source_ref,observed_at) VALUES(?,?,?,?,?)",
                    (f"s-{tid}", tid, src[0], src[1], c))


def status(con, tid):
    return con.execute("SELECT status FROM task WHERE id=?", (tid,)).fetchone()[0]


def test_add_and_complete(con):
    tid = task_ops.add(con, "  준비물 챙기기  ", NOW)
    assert con.execute("SELECT title, status FROM task WHERE id=?", (tid,)).fetchone()[:] == ("준비물 챙기기", "open")
    task_ops.complete(con, tid, NOW)
    assert status(con, tid) == "done"
    assert con.execute("SELECT completed_at FROM task WHERE id=?", (tid,)).fetchone()[0]
    task_ops.complete(con, tid, NOW)   # 멱등
    with pytest.raises(ValueError):
        task_ops.add(con, "   ", NOW)
    with pytest.raises(NotFound):
        task_ops.complete(con, "nope", NOW)


def test_confirm_candidate(con):
    mk(con, "c", "후보 일", status="candidate")
    task_ops.confirm(con, "c", NOW)
    assert status(con, "c") == "open"
    mk(con, "d", "끝난 일", status="done")
    with pytest.raises(IllegalTransition):
        task_ops.confirm(con, "d", NOW)


def test_drop_keeps_source_as_tombstone(con):
    mk(con, "t1", "일", src=("memo", "m5"))
    task_ops.drop(con, "t1", NOW)
    assert status(con, "t1") == "dropped"
    assert con.execute("SELECT COUNT(*) FROM task_source WHERE task_id='t1'").fetchone()[0] == 1
    assert task_ops.is_tombstoned(con, "memo", "m5")
    assert not task_ops.is_tombstoned(con, "memo", "m6")
    ev = con.execute("SELECT detail_json FROM audit_log WHERE event='task.drop'").fetchone()[0]
    assert json.loads(ev) == {"task_id": "t1"}


def test_drop_idempotent_and_missing(con):
    mk(con, "t1", "일")
    task_ops.drop(con, "t1", NOW)
    task_ops.drop(con, "t1", NOW)
    with pytest.raises(NotFound):
        task_ops.drop(con, "nope", NOW)


def test_drop_done_task_conflict(con):
    mk(con, "t1", "일", status="done")
    with pytest.raises(IllegalTransition):
        task_ops.drop(con, "t1", NOW)


def test_restore_within_24h_goes_to_candidate(con):
    mk(con, "t1", "일")
    task_ops.drop(con, "t1", NOW)
    task_ops.restore(con, "t1", NOW + timedelta(hours=23))
    assert status(con, "t1") == "candidate"


def test_restore_after_24h_rejected(con):
    mk(con, "t1", "일")
    task_ops.drop(con, "t1", NOW)
    with pytest.raises(IllegalTransition):
        task_ops.restore(con, "t1", NOW + timedelta(hours=25))
    assert status(con, "t1") == "dropped"


def test_restore_not_dropped_rejected(con):
    mk(con, "t1", "일")
    with pytest.raises(IllegalTransition):
        task_ops.restore(con, "t1", NOW)


def test_similar_bigram_examples():
    assert task_ops.similar("학교 준비물 사기", "학교 준비물사기") >= 0.6
    assert task_ops.similar("학교 준비물 사기", "학교 준비물 사기!") >= 0.9
    assert task_ops.similar("학교 알림장 확인", "보험 서류 제출") < 0.3
    assert task_ops.similar("", "") == 0.0


def test_suggestions_require_compatible_due_and_recent(con):
    d = NOW + timedelta(days=2)
    mk(con, "a", "학교 준비물 사기", due=d)
    mk(con, "b", "학교 준비물사기", due=d + timedelta(hours=1))      # 같은 날
    mk(con, "c", "학교 준비물사기", due=d + timedelta(days=3))       # 다른 날
    mk(con, "e", "학교 준비물사기", due=None)                        # 한쪽만 없음
    mk(con, "o", "학교 준비물사기", due=d, created=NOW - timedelta(days=20))   # 오래됨
    mk(con, "x", "학교 준비물사기", due=d, status="dropped")
    got = task_ops.suggestions(con, NOW)
    assert [frozenset((a, b)) for a, b, _ in got] == [frozenset(("a", "b"))]
    assert got[0][2] >= 0.6


def test_no_automatic_merge(con):
    mk(con, "m", "학교 준비물 사기", status="candidate", src=("memo", "th1"))
    mk(con, "c", "학교 준비물 사기", src=("calendar_event", "e1"))
    assert status(con, "m") == "candidate" and status(con, "c") == "open"
    assert len(task_ops.suggestions(con, NOW)) == 1


def test_merge_moves_sources_and_tombstones_other(con):
    mk(con, "k", "일", status="candidate", src=("memo", "th1"))
    mk(con, "o", "일", src=("calendar_event", "e1"), due=NOW + timedelta(days=1))
    con.execute("UPDATE task SET due_confidence='explicit' WHERE id='o'")
    task_ops.merge(con, "k", "o", NOW)
    assert status(con, "o") == "dropped"
    owners = {r[0]: r[1] for r in con.execute("SELECT source_ref, task_id FROM task_source")}
    assert owners == {"th1": "k", "e1": "k"}
    k = con.execute("SELECT status, due_at, due_confidence FROM task WHERE id='k'").fetchone()
    assert k["due_confidence"] == "explicit" and k["due_at"] and k["status"] == "open"
    ev = json.loads(con.execute("SELECT detail_json FROM audit_log WHERE event='task.merge'").fetchone()[0])
    assert ev == {"keep": "k", "other": "o"}


def test_merged_other_source_still_blocks_reregistration_if_keep_dropped(con):
    mk(con, "k", "일", src=("memo", "th1"))
    mk(con, "o", "일", src=("calendar_event", "e1"))
    task_ops.merge(con, "k", "o", NOW)
    task_ops.drop(con, "k", NOW)
    assert task_ops.is_tombstoned(con, "calendar_event", "e1")


def test_merge_errors(con):
    mk(con, "k", "일")
    mk(con, "d", "일", status="done")
    with pytest.raises(NotFound):
        task_ops.merge(con, "k", "zzz", NOW)
    with pytest.raises(IllegalTransition):
        task_ops.merge(con, "k", "k", NOW)
    with pytest.raises(IllegalTransition):
        task_ops.merge(con, "k", "d", NOW)


def _boom(*a, **k):
    raise RuntimeError("injected")


def test_merge_rolls_back_on_failure(con, monkeypatch):
    mk(con, "k", "일", src=("memo", "th1"))
    mk(con, "o", "일", src=("calendar_event", "e1"))
    monkeypatch.setattr(task_ops, "audit", _boom)
    with pytest.raises(RuntimeError):
        task_ops.merge(con, "k", "o", NOW)
    assert status(con, "o") == "open"
    assert {r[0]: r[1] for r in con.execute("SELECT source_ref, task_id FROM task_source")} == {"th1": "k", "e1": "o"}


def test_drop_rolls_back_on_failure(con, monkeypatch):
    mk(con, "k", "일")
    monkeypatch.setattr(task_ops, "audit", _boom)
    with pytest.raises(RuntimeError):
        task_ops.drop(con, "k", NOW)
    assert status(con, "k") == "open"


def test_restore_rolls_back_on_failure(con, monkeypatch):
    mk(con, "k", "일")
    task_ops.drop(con, "k", NOW)
    monkeypatch.setattr(task_ops, "audit", _boom)
    with pytest.raises(RuntimeError):
        task_ops.restore(con, "k", NOW)
    assert status(con, "k") == "dropped"


def test_restore_rejected_for_merged_task(con):
    mk(con, "k", "일")
    mk(con, "o", "일")
    task_ops.merge(con, "k", "o", NOW)
    with pytest.raises(IllegalTransition):
        task_ops.restore(con, "o", NOW)
    assert status(con, "o") == "dropped"

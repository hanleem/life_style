import sqlite3
import threading
import time

import pytest

from pa.backup import snapshot
from pa.backup.snapshot import SnapshotError, inspect_db, take_snapshot
from pa.db import SCHEMA_VERSION, connect, init_db


def _live(tmp_path, name="core.db"):
    c = connect(tmp_path / name)
    init_db(c)
    return c


def _audit(c, i, detail="x"):
    c.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES(?,?,?,?,?)",
              (f"id{i}", "2026-10-06T00:00:00+00:00", "t", "e", detail))


def _count(path, table="audit_log"):
    c = sqlite3.connect(str(path))
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def test_live_db_is_wal(tmp_path):
    c = _live(tmp_path)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_snapshot_includes_committed_wal_content(tmp_path):
    c = _live(tmp_path)
    c.execute("PRAGMA wal_autocheckpoint=0")
    _audit(c, 1)
    assert (tmp_path / "core.db-wal").stat().st_size > 0
    snap = take_snapshot(tmp_path / "core.db", tmp_path / "work", "core")
    assert _count(snap.path) == 1
    assert snap.tables["audit_log"] == 1


def test_snapshot_while_writer_active_is_consistent(tmp_path):
    c = _live(tmp_path)
    stop = threading.Event()
    worst = [0.0]
    n = [0]

    def writer():
        w = connect(tmp_path / "core.db")
        i = 0
        while not stop.is_set():
            t = time.monotonic()
            _audit(w, 1000 + i)
            worst[0] = max(worst[0], time.monotonic() - t)
            i += 1
            n[0] = i
        w.close()

    th = threading.Thread(target=writer)
    th.start()
    time.sleep(0.2)
    snap = take_snapshot(tmp_path / "core.db", tmp_path / "work", "core")
    stop.set()
    th.join()
    c2 = sqlite3.connect(str(snap.path))
    assert c2.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    c2.close()
    assert 0 <= snap.tables["audit_log"] <= n[0]
    assert worst[0] < 5


def test_snapshot_does_not_block_writer_long(tmp_path, monkeypatch):
    c = _live(tmp_path)
    for i in range(50):
        _audit(c, i)
    done = {}

    def run():
        done["snap"] = take_snapshot(tmp_path / "core.db", tmp_path / "work", "core")

    th = threading.Thread(target=run)
    th.start()
    t = time.monotonic()
    c.execute("BEGIN IMMEDIATE")
    _audit(c, 9999)
    c.execute("COMMIT")
    assert time.monotonic() - t < 1.0
    th.join()
    assert "snap" in done


def test_snapshot_drops_deleted_row_remnants(tmp_path):
    c = _live(tmp_path)
    # 온라인 백업 API(Connection.backup)는 빈 페이지까지 그대로 복사하므로 삭제된 행 조각이 남을 수 있다.
    # VACUUM INTO 는 살아 있는 행만 새 파일에 쓴다.
    _audit(c, 1, "MARKER-DELETED-XYZ" * 200)
    _audit(c, 2, "keep")
    c.execute("DELETE FROM audit_log WHERE id='id1'")
    snap = take_snapshot(tmp_path / "core.db", tmp_path / "work", "core")
    assert b"MARKER-DELETED-XYZ" not in snap.path.read_bytes()
    assert snap.tables["audit_log"] == 1


def test_absent_source_raises_absent(tmp_path):
    with pytest.raises(SnapshotError) as e:
        take_snapshot(tmp_path / "nope.db", tmp_path / "work", "core")
    assert e.value.code == "absent"


def test_sqlite_too_old_refuses(tmp_path, monkeypatch):
    _live(tmp_path)
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 26, 0))
    assert snapshot.sqlite_supported() is False
    with pytest.raises(SnapshotError) as e:
        take_snapshot(tmp_path / "core.db", tmp_path / "work", "core")
    assert e.value.code == "sqlite_old"


def test_corrupt_source_raises_and_cleans_work(tmp_path):
    c = _live(tmp_path)
    for i in range(300):
        _audit(c, i, "z" * 500)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    p = tmp_path / "core.db"
    # 테이블 본문 페이지(audit_log 행이 든 리프) 중 가운데 것의 머리를 깬다. 인덱스 페이지는 VACUUM INTO 가 다시 만들어
    # 손상을 못 보므로 스키마가 바뀌어도 데이터 페이지를 직접 찾아 쓴다.
    raw = p.read_bytes()
    pages = [i for i in range(1, len(raw) // 4096) if b"z" * 400 in raw[i * 4096:(i + 1) * 4096]]
    assert pages
    with open(p, "r+b") as f:
        f.seek(pages[len(pages) // 2] * 4096)
        f.write(b"\xff" * 64)
    with pytest.raises(SnapshotError) as e:
        take_snapshot(p, tmp_path / "work", "core")
    assert e.value.code in {"integrity", "io", "unexpected"}
    assert list((tmp_path / "work").glob("*")) == []
    assert "core.db" not in str(e.value) and str(tmp_path) not in str(e.value)


def test_locked_source_raises_busy(tmp_path, monkeypatch):
    p = tmp_path / "core.db"
    c = sqlite3.connect(str(p), isolation_level=None)
    c.execute("CREATE TABLE t(a)")
    c.execute("INSERT INTO t VALUES(1)")
    monkeypatch.setattr(snapshot, "BUSY_TIMEOUT_S", 0.1)
    c.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(SnapshotError) as e:
            take_snapshot(p, tmp_path / "work", "core")
        assert e.value.code == "busy"
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert list((tmp_path / "work").glob("*")) == []


def test_snapshot_counts_and_user_version(tmp_path):
    c = _live(tmp_path)
    for i in range(3):
        _audit(c, i)
    snap = take_snapshot(tmp_path / "core.db", tmp_path / "work", "core")
    assert snap.user_version == SCHEMA_VERSION
    assert snap.tables["audit_log"] == 3
    assert snap.bytes == snap.path.stat().st_size
    assert inspect_db(snap.path) == (snap.user_version, snap.tables)


def test_snapshot_never_modifies_source(tmp_path):
    c = _live(tmp_path)
    _audit(c, 1)

    def state():
        return (c.execute("PRAGMA user_version").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
                [tuple(r) for r in c.execute("SELECT sql FROM sqlite_master ORDER BY name")])
    before = state()
    take_snapshot(tmp_path / "core.db", tmp_path / "work", "core")
    assert state() == before

import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pa.backup import store

T0 = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)


def mk_run(root, when, files=("core",), key_fp="a" * 16):
    rid = store.new_run_id(when, root)
    meta_files = {}
    with store.RunWriter(root, rid) as w:
        for k in files:
            data = f"{k}-{rid}".encode()
            w.path_for(store.DB_FILES[k]).write_bytes(data)
            meta_files[k] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        w.path_for(store.MANIFEST).write_bytes(b"manifest")
        w.commit({"format": 1, "run_id": rid, "created_at": when.isoformat(), "key_fp": key_fp,
                  "files": meta_files})
    return rid


def test_commit_is_atomic_rename(tmp_path):
    rid = store.new_run_id(T0, tmp_path)
    with store.RunWriter(tmp_path, rid) as w:
        w.path_for("core.pabk").write_bytes(b"x")
        assert store.list_runs(tmp_path) == []
        assert (tmp_path / f".partial-{rid}").is_dir()
        w.commit({"format": 1, "run_id": rid, "created_at": T0.isoformat(), "key_fp": "f" * 16,
                  "files": {"core": {"bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}}})
    runs = store.list_runs(tmp_path)
    assert [r.run_id for r in runs] == [rid]
    assert not (tmp_path / f".partial-{rid}").exists()


def test_exception_inside_writer_leaves_no_final_dir(tmp_path):
    rid = store.new_run_id(T0, tmp_path)
    with pytest.raises(RuntimeError):
        with store.RunWriter(tmp_path, rid) as w:
            w.path_for("core.pabk").write_bytes(b"x")
            raise RuntimeError("boom")
    assert [p.name for p in tmp_path.iterdir()] == []


def test_killed_mid_write_partial_is_invisible_and_cleaned(tmp_path):
    p = tmp_path / ".partial-20261006T170000Z"
    p.mkdir()
    (p / "core.pabk").write_bytes(b"x")
    assert store.list_runs(tmp_path) == []
    old = (T0 - timedelta(hours=2)).timestamp()
    os.utime(p, (old, old))
    assert store.cleanup_stale(tmp_path, T0) == 1
    assert not p.exists()
    q = tmp_path / ".partial-20261006T175000Z"
    q.mkdir()
    new = (T0 - timedelta(minutes=30)).timestamp()
    os.utime(q, (new, new))
    assert store.cleanup_stale(tmp_path, T0) == 0
    assert q.exists()


def test_list_ignores_foreign_and_malformed(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.txt").write_text("a")
    (tmp_path / "20261006T180000Z").mkdir()
    (tmp_path / "20261006T180000Z" / "core.pabk").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "20261005T180000Z").symlink_to(outside, target_is_directory=True)
    except OSError:
        pass
    assert store.list_runs(tmp_path) == []
    assert store.list_runs(root) == []
    assert (tmp_path / "notes" / "a.txt").exists()
    assert (tmp_path / "20261006T180000Z" / "core.pabk").exists()


@pytest.mark.parametrize("name", ["../data", "..\\data", "20261006T180000Z/../../x", "/etc", "C:\\x",
                                  "20261006T180000Z\x00", " 20261006T180000Z", "20261006T180000Z\n",
                                  "\\\\server\\share", "\u2024\u2024"])
def test_resolve_run_rejects_traversal(tmp_path, name):
    with pytest.raises(ValueError):
        store.resolve_run(tmp_path, name)


def test_resolve_run_missing_and_ok(tmp_path):
    with pytest.raises(FileNotFoundError):
        store.resolve_run(tmp_path, "20261006T180000Z")
    rid = mk_run(tmp_path, T0)
    assert store.resolve_run(tmp_path, rid) == (tmp_path / rid).resolve()


def _daily(root, n, files=("core",)):
    return [mk_run(root, T0 - timedelta(days=i), files) for i in range(n - 1, -1, -1)]


def test_core_keeps_7_daily_4_weekly(tmp_path):
    _daily(tmp_path, 60, ("core",))
    plan = store.plan_retention(store.list_runs(tmp_path), T0)
    store.apply_retention(tmp_path, plan)
    left = store.list_runs(tmp_path)
    assert 7 <= len(left) <= 11
    assert max(T0 - r.created_at for r in left) <= timedelta(days=35)
    kst = {r.created_at.astimezone(store.KST).date() for r in left}
    for i in range(7):
        assert (T0 - timedelta(days=i)).astimezone(store.KST).date() in kst


def test_core_same_day_multiple_runs_keeps_latest(tmp_path):
    ids = [mk_run(tmp_path, T0 - timedelta(hours=h), ("core",)) for h in (3, 2, 1)]
    plan = store.plan_retention(store.list_runs(tmp_path), T0)
    assert set(plan.delete_runs) == set(ids[:2])
    # 같은 KST 날짜(10/6 15:00-17:00 UTC = 10/7 00:00-02:00 KST)의 최신(1시간 전)만 남는다
    assert ids[2] not in plan.delete_runs


def test_run_dir_removed_when_empty(tmp_path):
    ids = _daily(tmp_path, 60, ("core",))
    old = ids[0]                       # 가장 오래된 것(59일 전)
    plan = store.plan_retention(store.list_runs(tmp_path), T0)
    assert old in plan.delete_runs
    store.apply_retention(tmp_path, plan)
    assert not (tmp_path / old).exists()


def test_apply_retention_only_touches_known_names(tmp_path):
    ids = _daily(tmp_path, 60, ("core",))
    old = ids[0]
    (tmp_path / old / "readme.txt").write_text("keep me")
    plan = store.plan_retention(store.list_runs(tmp_path), T0)
    store.apply_retention(tmp_path, plan)
    assert (tmp_path / old / "readme.txt").exists()
    assert not (tmp_path / old / "core.pabk").exists()


def test_apply_retention_updates_run_json(tmp_path):
    ids = _daily(tmp_path, 60, ("core", "google_token"))
    plan = store.plan_retention(store.list_runs(tmp_path), T0)
    partial = [rid for kind, rid in plan.delete_files if kind == "google_token"]
    assert partial
    store.apply_retention(tmp_path, plan)
    for rid in ids:
        if (tmp_path / rid / "run.json").exists():
            meta = json.loads((tmp_path / rid / "run.json").read_text())
            for kind in meta["files"]:
                assert (tmp_path / rid / store.DB_FILES[kind]).exists()


def test_lock_single_flight_and_stale(tmp_path):
    l1 = store.acquire_lock(tmp_path, T0)
    assert l1 is not None
    assert store.acquire_lock(tmp_path, T0) is None
    l1.release()
    assert store.acquire_lock(tmp_path, T0) is not None
    (tmp_path / store.LOCK).unlink()
    store.acquire_lock(tmp_path, T0 - timedelta(hours=3))
    l3 = store.acquire_lock(tmp_path, T0)
    assert l3 is not None
    with l3:
        pass
    assert not (tmp_path / store.LOCK).exists()


def test_mirror_absent_never_created(tmp_path):
    m = tmp_path / "E" / "PA-backup"
    assert store.mirror_ready(m) is False
    assert not m.exists() and not m.parent.exists()


def test_mirror_requires_marker(tmp_path):
    m = tmp_path / "m"
    m.mkdir()
    assert store.mirror_ready(m) is False
    store.init_mirror(m)
    assert store.mirror_ready(m) is True
    store.init_mirror(m)  # 멱등
    other = tmp_path / "other"
    other.mkdir()
    (other / "file.txt").write_text("x")
    with pytest.raises(store.UnsafeLocation):
        store.init_mirror(other)


def test_copy_run_ok_and_verifies_hash(tmp_path, monkeypatch):
    root, m = tmp_path / "root", tmp_path / "m"
    m.mkdir()
    store.init_mirror(m)
    rid = mk_run(root, T0)
    store.copy_run(root, rid, m)
    assert (m / rid / "core.pabk").read_bytes() == (root / rid / "core.pabk").read_bytes()
    rid2 = mk_run(root, T0 + timedelta(days=1))
    real = shutil.copyfile

    def bad(src, dst, *a, **k):
        r = real(src, dst, *a, **k)
        if str(dst).endswith("core.pabk"):
            with open(dst, "ab") as f:
                f.write(b"!")
        return r
    monkeypatch.setattr(shutil, "copyfile", bad)
    with pytest.raises(Exception):
        store.copy_run(root, rid2, m)
    assert not (m / rid2).exists()
    assert not list(m.glob(".partial-*"))


def test_check_location_rules(tmp_path):
    repo = tmp_path / "repo"
    data = repo / "data"
    (repo / "pa" / "web" / "static").mkdir(parents=True)
    data.mkdir()
    kw = dict(data_dir=data, repo_root=repo)
    for bad in (repo / "pa" / "web" / "static" / "b", repo / "backups", data, data / ".backup-work" / "x"):
        with pytest.raises(store.UnsafeLocation):
            store.check_location(bad, **kw)
    store.check_location(data / "backups", **kw)
    store.check_location(tmp_path / "elsewhere", **kw)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_harden_posix_modes(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    f = d / "f"
    f.write_text("x")
    store.harden(d)
    store.harden(f)
    assert d.stat().st_mode & 0o777 == 0o700
    assert f.stat().st_mode & 0o777 == 0o600


def test_runwriter_dir_is_private(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX")
    rid = store.new_run_id(T0, tmp_path)
    with store.RunWriter(tmp_path, rid):
        assert (tmp_path / f".partial-{rid}").stat().st_mode & 0o777 == 0o700


def test_new_run_id_increments_on_collision(tmp_path):
    a = store.new_run_id(T0, tmp_path)
    assert a == "20261006T180000Z"
    (tmp_path / a).mkdir()
    assert store.new_run_id(T0, tmp_path) == "20261006T180001Z"


def test_purge_pre_restore_after_14_days(tmp_path):
    old = tmp_path / "pre-restore-20260920T000000Z"
    new = tmp_path / "pre-restore-20261005T000000Z"
    other = tmp_path / "keepme"
    for d in (old, new, other):
        d.mkdir()
        (d / "core.db").write_text("x")
    assert store.purge_pre_restore(tmp_path, T0) == 1
    assert not old.exists() and new.exists() and other.exists()

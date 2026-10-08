import os
import sqlite3
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from backup_helpers import T0, make_run
from pa.backup import crypt, restore
from pa.backup.restore import RestoreRefused
from pa.db import SCHEMA_VERSION, connect, init_db


def _rt(r, to, **kw):
    return restore.restore_to(r["root"], r["run_id"], kw.pop("key", r["key"]), to, work_dir=r["work"],
                              data_dir=r["data"], **kw)


def test_restore_to_new_dir_opens_and_counts_match(tmp_path):
    r = make_run(tmp_path)
    to = tmp_path / "out"
    res = _rt(r, to)
    assert set(res.files) == {"core.db"}
    c = connect(to / "core.db")
    init_db(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == r["manifest"]["core"]["tables"]["audit_log"]
    c.close()
    assert not list(to.glob(".partial*")) and not (to / "core.db-wal").exists()
    if os.name != "nt":
        assert (to / "core.db").stat().st_mode & 0o777 == 0o600


def test_restore_refuses_nonempty_target(tmp_path):
    r = make_run(tmp_path)
    to = tmp_path / "out"
    to.mkdir()
    (to / "x").write_text("x")
    with pytest.raises(RestoreRefused) as e:
        _rt(r, to)
    assert e.value.code == "not_empty"


def test_restore_refuses_target_inside_data_dir(tmp_path):
    r = make_run(tmp_path)
    with pytest.raises(RestoreRefused) as e:
        _rt(r, r["data"] / "sub")
    assert e.value.code == "unsafe_target"
    assert not (r["data"] / "sub").exists()


def test_restore_refuses_target_inside_backup_root(tmp_path):
    r = make_run(tmp_path)
    with pytest.raises(RestoreRefused) as e:
        _rt(r, r["root"] / "sub")
    assert e.value.code == "unsafe_target"


def test_restore_refuses_target_inside_mirror(tmp_path):
    r = make_run(tmp_path)
    m = tmp_path / "mirror"
    m.mkdir()
    with pytest.raises(RestoreRefused) as e:
        _rt(r, m / "sub", mirror=m)
    assert e.value.code == "unsafe_target"


def test_restore_wrong_key_writes_nothing(tmp_path):
    r = make_run(tmp_path)
    to = tmp_path / "out"
    with pytest.raises(RestoreRefused) as e:
        _rt(r, to, key=Fernet.generate_key().decode())
    assert e.value.code == "verify" and "key" in e.value.reasons
    assert not to.exists() or not list(to.iterdir())


def test_restore_only_core(tmp_path):
    r = make_run(tmp_path)
    to = tmp_path / "out"
    _rt(r, to, only=("core",))
    assert sorted(p.name for p in to.iterdir()) == ["core.db"]


def test_restore_only_missing_kind_refused(tmp_path):
    r = make_run(tmp_path)
    with pytest.raises(RestoreRefused) as e:
        _rt(r, tmp_path / "out", only=("google_token",))
    assert e.value.code == "missing"


def test_restore_name_traversal_rejected(tmp_path):
    r = make_run(tmp_path)
    with pytest.raises(ValueError):
        restore.restore_to(r["root"], "../x", r["key"], tmp_path / "o", work_dir=r["work"], data_dir=r["data"])


def _live_with_wal(r):
    """라이브 core.db 에 백업 이후 행을 추가하고 -wal/-shm 이 남아 있는 상태를 만든다."""
    c = sqlite3.connect(r["data"] / "core.db", isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES('late','x','t','e','{}')")
    return c   # 닫지 않아 -wal/-shm 이 남는다


def _rl(r, **kw):
    kw.setdefault("is_running", lambda: False)
    return restore.replace_live(r["root"], r["run_id"], r["key"], r["data"], now=T0 + timedelta(hours=1),
                                work_dir=r["work"], **kw)


def test_replace_live_refuses_when_server_running(tmp_path):
    r = make_run(tmp_path)
    before = (r["data"] / "core.db").read_bytes()
    with pytest.raises(RestoreRefused) as e:
        _rl(r, is_running=lambda: True)
    assert e.value.code == "server_running"
    assert (r["data"] / "core.db").read_bytes() == before


def test_replace_live_uses_module_server_running_by_default(tmp_path, monkeypatch):
    r = make_run(tmp_path)
    monkeypatch.setattr(restore, "server_running", lambda port=8010: True)
    with pytest.raises(RestoreRefused):
        restore.replace_live(r["root"], r["run_id"], r["key"], r["data"], now=T0, work_dir=r["work"])


@pytest.mark.skipif(os.name == "nt", reason="열린 SQLite 핸들이 있는 파일은 Windows 에서 옮길 수 없다(리눅스에서 검증)")
def test_replace_live_moves_db_wal_shm_together(tmp_path):
    r = make_run(tmp_path)
    keep = _live_with_wal(r)
    assert (r["data"] / "core.db-wal").exists()
    res = _rl(r)
    keep.close()
    pre = res.pre_restore
    assert pre.name.startswith("pre-restore-")
    for n in ("core.db", "core.db-wal", "core.db-shm"):
        assert (pre / n).exists(), n
    assert not (r["data"] / "core.db-wal").exists() and not (r["data"] / "core.db-shm").exists()
    c = sqlite3.connect(r["data"] / "core.db")
    assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == r["manifest"]["core"]["tables"]["audit_log"]
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE id='late'").fetchone()[0] == 0
    c.close()
    old = sqlite3.connect(pre / "core.db")
    assert old.execute("SELECT COUNT(*) FROM audit_log WHERE id='late'").fetchone()[0] == 1
    old.close()
    assert not list(r["data"].glob("*.restore-tmp"))


def test_replace_live_rollback_on_failure(tmp_path, monkeypatch):
    r = make_run(tmp_path)
    before = {n: (r["data"] / n).read_bytes() for n in ("core.db",)}
    real = crypt.decrypt_file

    def boom(src, dst, key):
        if str(dst).endswith(".restore-tmp"):
            raise OSError(28, "No space left on device")
        return real(src, dst, key)
    monkeypatch.setattr(crypt, "decrypt_file", boom)
    with pytest.raises(OSError):
        _rl(r)
    for n, b in before.items():
        assert (r["data"] / n).read_bytes() == b
    assert not list(r["data"].glob("*.restore-tmp"))
    assert not list(r["data"].glob("pre-restore-*"))


def test_replace_live_rollback_when_replace_step_fails(tmp_path, monkeypatch):
    r = make_run(tmp_path)
    keep = _live_with_wal(r)
    keep.close()
    before = {n: (r["data"] / n).read_bytes() for n in ("core.db",)}
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        if str(b) == str(r["data"] / "core.db") and calls["n"] == 0:   # 첫 교체만 실패(되돌리기는 성공해야 한다)
            calls["n"] += 1
            raise OSError(5, "io")
        return real(a, b)
    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        _rl(r)
    monkeypatch.undo()
    assert calls["n"] == 1
    for n, b in before.items():
        assert (r["data"] / n).read_bytes() == b


def test_replace_live_refuses_on_verify_failure(tmp_path):
    r = make_run(tmp_path)
    p = r["root"] / r["run_id"] / "core.pabk"
    p.write_bytes(p.read_bytes() + b"x")
    before = (r["data"] / "core.db").read_bytes()
    with pytest.raises(RestoreRefused) as e:
        _rl(r)
    assert e.value.code == "verify"
    assert (r["data"] / "core.db").read_bytes() == before
    assert not list(r["data"].glob("pre-restore-*"))


def test_server_running_detects_listener():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert restore.server_running(port) is True
    finally:
        s.close()
    assert restore.server_running(port) is False

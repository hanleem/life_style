import json
import os
import shutil
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from backup_helpers import T0, make_live
from pa.backup import copy as bcopy
from pa.backup import crypt, runner, store, verify
from pa.core.coach_copy import notify_safe
from pa.settings import Settings


def mk(tmp_path, **kw):
    key = Fernet.generate_key().decode()
    data = tmp_path / "data"
    make_live(data)
    s = Settings(data_dir=data, backup_key=kw.pop("backup_key", key), _env_file=None, **kw)
    return s, key, None


def test_full_run_ok_and_verifiable(tmp_path):
    s, key, _ = mk(tmp_path)
    res = runner.run_backup(s, T0)
    assert (res.result, res.reason, res.files) == ("ok", "", 1)
    runs = store.list_runs(s.backup_root)
    assert len(runs) == 1 and res.run_id == runs[0].run_id
    rep = verify.verify_run(s.backup_root, res.run_id, key, work_dir=s.backup_work_dir)
    assert rep.ok
    assert res.bytes > 0 and res.mirror == "off"
    assert not list(s.backup_work_dir.glob("*")) and not (s.backup_root / ".lock").exists()


def test_no_key_skips_and_writes_nothing(tmp_path):
    s, _, _ = mk(tmp_path, backup_key="")
    res = runner.run_backup(s, T0)
    assert (res.result, res.reason) == ("skipped", "no_key")
    assert not s.backup_root.exists() and not s.backup_work_dir.exists()


def test_bad_key_fails_fast(tmp_path):
    s, _, _ = mk(tmp_path, backup_key="not-a-key")
    res = runner.run_backup(s, T0)
    assert (res.result, res.reason) == ("failed", "bad_key")
    assert not s.backup_root.exists()


def test_allowlist_only(tmp_path):
    s, _, _ = mk(tmp_path)
    (s.data_dir / "voice").mkdir()
    (s.data_dir / "voice" / "a.wav").write_bytes(b"x")
    (s.data_dir / "google_token.enc").write_bytes(b"tok")
    (s.data_dir / ".env").write_text("PA_SESSION_SECRET=SENTINEL")
    res = runner.run_backup(s, T0)
    names = sorted(p.name for p in (s.backup_root / res.run_id).iterdir())
    assert names == ["core.pabk", "manifest.pabk", "run.json"]


def test_google_token_included_when_enabled(tmp_path):
    s, key, _ = mk(tmp_path, backup_include_google_token=True)
    (s.data_dir / "google_token.enc").write_bytes(b"tok-bytes")
    res = runner.run_backup(s, T0)
    assert res.files == 2
    out = tmp_path / "t"
    crypt.decrypt_file(s.backup_root / res.run_id / "google_token.pabk", out, key)
    assert out.read_bytes() == b"tok-bytes"
    assert verify.verify_run(s.backup_root, res.run_id, key, work_dir=s.backup_work_dir).ok


def test_disk_full_mid_write(tmp_path, monkeypatch):
    s, _, _ = mk(tmp_path, backup_include_google_token=True)
    (s.data_dir / "google_token.enc").write_bytes(b"tok")
    real, n = crypt.encrypt_file, [0]

    def flaky(src, dst, key):
        n[0] += 1
        if n[0] == 2:
            raise OSError(28, "No space left on device")
        return real(src, dst, key)
    monkeypatch.setattr(crypt, "encrypt_file", flaky)
    res = runner.run_backup(s, T0)
    assert (res.result, res.reason, res.files) == ("partial", "io", 1)
    assert len(store.list_runs(s.backup_root)) == 1
    assert not list(s.backup_root.glob(".partial-*"))
    assert not list(s.backup_work_dir.glob("*"))
    run = s.backup_root / res.run_id
    assert (run / "core.pabk").exists() and not (run / "google_token.pabk").exists()


def test_disk_full_first_db_and_second(tmp_path, monkeypatch):
    s, _, _ = mk(tmp_path)

    def boom(src, dst, key):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(crypt, "encrypt_file", boom)
    res = runner.run_backup(s, T0)
    assert (res.result, res.reason) == ("failed", "io")
    assert store.list_runs(s.backup_root) == []
    assert not list(s.backup_root.glob(".partial-*")) and not list(s.backup_root.glob("2*"))
    assert not list(s.backup_work_dir.glob("*"))


def test_too_large_skipped(tmp_path):
    s, _, _ = mk(tmp_path, backup_max_mb=0)
    res = runner.run_backup(s, T0)
    assert (res.result, res.reason) == ("failed", "too_large")
    assert store.list_runs(s.backup_root) == []


def test_verify_failure_removes_new_run_keeps_old(tmp_path, monkeypatch):
    s, _, _ = mk(tmp_path)
    first = runner.run_backup(s, T0)
    assert first.result == "ok"
    monkeypatch.setattr(verify, "verify_run",
                        lambda *a, **k: verify.VerifyReport(False, {"core": verify.FileCheck(False, "hash")}))
    res = runner.run_backup(s, T0 + timedelta(hours=1))
    assert (res.result, res.reason) == ("failed", "verify")
    assert [r.run_id for r in store.list_runs(s.backup_root)] == [first.run_id]


def test_locked_skips(tmp_path):
    s, _, _ = mk(tmp_path)
    s.backup_root.mkdir(parents=True)
    (s.backup_root / ".lock").write_text(T0.isoformat())
    res = runner.run_backup(s, T0)
    assert (res.result, res.reason) == ("skipped", "locked")
    assert (s.backup_root / ".lock").exists()    # 남의 잠금을 지우지 않는다


def test_work_dir_leftover_plaintext_removed_on_next_run(tmp_path):
    s, _, _ = mk(tmp_path)
    s.backup_work_dir.mkdir(parents=True)
    (s.backup_work_dir / "core-xxx.db").write_bytes(b"PLAINTEXT")
    runner.run_backup(s, T0)
    assert not list(s.backup_work_dir.glob("*"))


def test_never_raises(tmp_path, monkeypatch):
    s, _, _ = mk(tmp_path)

    def boom(root):
        raise RuntimeError("secret path /x/y")
    monkeypatch.setattr(store, "list_runs", boom)
    res = runner.run_backup(s, T0)
    assert (res.result, res.reason) == ("failed", "unexpected")
    assert not (s.backup_root / ".lock").exists()


def test_unsafe_location_refused(tmp_path):
    s, _, _ = mk(tmp_path, backup_dir=tmp_path / "data")
    res = runner.run_backup(s, T0)
    assert (res.result, res.reason) == ("failed", "unsafe_location")


def test_mirror_missing_does_not_fail_primary(tmp_path):
    m = tmp_path / "E" / "PA-backup"
    s, _, _ = mk(tmp_path, backup_mirror_dir=m)
    res = runner.run_backup(s, T0)
    assert (res.result, res.mirror) == ("ok", "missing")
    assert not m.exists() and not m.parent.exists()


def _mirror(tmp_path):
    m = tmp_path / "mirror"
    m.mkdir()
    store.init_mirror(m)
    return m


def test_mirror_ok_copies_core(tmp_path):
    m = _mirror(tmp_path)
    s, key, _ = mk(tmp_path, backup_mirror_dir=m)
    res = runner.run_backup(s, T0)
    assert res.mirror == "ok" and (m / res.run_id / "core.pabk").exists()
    assert store.list_runs(m)[0].run_id == res.run_id


# ---- record_result / status ----

def _res(**kw):
    base = dict(run_id="20261006T180000Z", result="ok", reason="", files=2, bytes=4404019, ms=1200,
                mirror="off", purged=0)
    base.update(kw)
    return runner.RunResult(**base)


def test_record_result_audit_has_no_paths(con, tmp_path):
    runner.record_result(con, _res(), T0)
    row = con.execute("SELECT detail_json FROM audit_log WHERE event='backup.run'").fetchone()
    d = json.loads(row[0])
    assert set(d) == {"run_id", "result", "reason", "files", "bytes", "ms", "mirror", "purged"}
    txt = row[0]
    for bad in (str(tmp_path), ".pabk", "journal", "audit_log", "core.pabk"):
        assert bad not in txt


def test_record_result_prefs_and_status(con):
    runner.record_result(con, _res(), T0)
    st = runner.status(con, enabled=True)
    assert st.enabled and st.last_ok_at == T0 and st.last_bytes == 4404019 and st.last_result == "ok"
    assert con.execute("SELECT value FROM preference WHERE key='backup.ever_ok'").fetchone()[0] == "1"
    runner.record_result(con, _res(result="failed", reason="io", run_id=None, files=0, bytes=0), T0 + timedelta(hours=1))
    st = runner.status(con, enabled=True)
    assert st.last_result == "failed" and st.last_reason == "io" and st.last_ok_at == T0


def test_record_result_attempts_reset_by_kst_date(con):
    def attempts():
        return con.execute("SELECT value FROM preference WHERE key='backup.attempts'").fetchone()[0]
    runner.record_result(con, _res(result="failed", reason="io"), T0)
    runner.record_result(con, _res(result="failed", reason="io"), T0 + timedelta(hours=1))
    assert attempts() == "2026-10-07:2"
    runner.record_result(con, _res(result="failed", reason="io"), T0 + timedelta(days=1))
    assert attempts() == "2026-10-08:1"
    runner.record_result(con, _res(), T0 + timedelta(days=1), attempt=False)
    assert attempts() == "2026-10-08:1"


def test_record_result_locked_does_not_overwrite_state(con):
    runner.record_result(con, _res(), T0)
    runner.record_result(con, _res(result="skipped", reason="locked", run_id=None), T0 + timedelta(minutes=1))
    assert runner.status(con, enabled=True).last_result == "ok"


def test_mirror_missing_since_set_and_cleared(con):
    runner.record_result(con, _res(mirror="missing"), T0)
    since = runner.status(con, enabled=True).mirror_missing_since
    assert since == T0
    runner.record_result(con, _res(mirror="missing"), T0 + timedelta(days=1))
    assert runner.status(con, enabled=True).mirror_missing_since == T0
    runner.record_result(con, _res(mirror="ok"), T0 + timedelta(days=2))
    assert runner.status(con, enabled=True).mirror_missing_since is None


def test_run_backup_has_no_con_param():
    import inspect
    assert list(inspect.signature(runner.run_backup).parameters) == ["settings", "now"]


def test_copy_strings_notify_safe():
    vals = [v for k, v in vars(bcopy).items() if k.isupper() and isinstance(v, str)]
    vals += list(bcopy.REASON_TEXT.values())
    assert vals and all(notify_safe(v) for v in vals)
    assert set(runner.REASONS) == set(bcopy.REASON_TEXT)
    assert bcopy.BACKUP_CHECK == "백업을 확인해 주세요"


def test_logs_have_no_paths(tmp_path, monkeypatch, caplog):
    s, key, _ = mk(tmp_path)
    monkeypatch.setattr(store, "list_runs", lambda r: (_ for _ in ()).throw(RuntimeError(str(tmp_path))))
    with caplog.at_level("DEBUG"):
        runner.run_backup(s, T0)
    assert str(tmp_path) not in caplog.text and key not in caplog.text

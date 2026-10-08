import json
import sqlite3

import pytest
from cryptography.fernet import Fernet

from backup_helpers import T0, make_run, rewrite_run_json, sha
from pa.backup import crypt, store, verify
from pa.db import SCHEMA_VERSION


def _v(r, **kw):
    kw.setdefault("work_dir", r["work"])
    return verify.verify_run(r["root"], r["run_id"], kw.pop("key", r["key"]), **kw)


def _no_db(work):
    return not work.exists() or not list(work.glob("*"))


def test_verify_ok_roundtrip(tmp_path):
    r = make_run(tmp_path)
    rep = _v(r)
    assert rep.ok
    assert set(rep.files) == {"core"}
    assert rep.files["core"].tables == r["manifest"]["core"]["tables"]
    assert rep.files["core"].user_version == SCHEMA_VERSION


def test_verify_wrong_backup_key(tmp_path):
    r = make_run(tmp_path)
    rep = _v(r, key=Fernet.generate_key().decode())
    assert not rep.ok
    assert {f.reason for f in rep.files.values()} == {"key"}


def test_verify_invalid_backup_key_format(tmp_path):
    r = make_run(tmp_path)
    rep = _v(r, key="not-a-key")
    assert not rep.ok and {f.reason for f in rep.files.values()} == {"key"}


def test_verify_detects_corrupted_snapshot(tmp_path):
    r = make_run(tmp_path)
    p = r["root"] / r["run_id"] / "core.pabk"
    b = bytearray(p.read_bytes())
    b[len(b) // 2] ^= 0xFF
    p.write_bytes(bytes(b))
    rep = _v(r)
    assert not rep.ok and rep.files["core"].reason == "hash"


def test_verify_detects_hash_ok_but_bad_db(tmp_path):
    r = make_run(tmp_path)
    plain = tmp_path / "plain.db"
    crypt.decrypt_file(r["root"] / r["run_id"] / "core.pabk", plain, r["key"])
    raw = bytearray(plain.read_bytes())
    for i in range(len(raw) // 2, len(raw) // 2 + 4096):
        raw[i] = 0xFF
    plain.write_bytes(bytes(raw))
    dst = r["root"] / r["run_id"] / "core.pabk"
    dst.unlink()
    info = crypt.encrypt_file(plain, dst, r["key"])
    rewrite_run_json(r["root"], r["run_id"],
                     lambda m: m["files"]["core"].update(sha256=info.sha256, bytes=info.bytes_out))
    rep = _v(r)
    assert not rep.ok and rep.files["core"].reason in {"integrity", "schema", "counts", "corrupt"}


def _swap_plain(r, kind, mutate):
    """평문으로 복호화 -> mutate(sqlite 연결) -> 다시 암호화하고 run.json 해시를 갱신."""
    plain = r["root"].parent / f"plain-{kind}.db"
    dst = r["root"] / r["run_id"] / f"{kind}.pabk"
    crypt.decrypt_file(dst, plain, r["key"])
    c = sqlite3.connect(plain)
    mutate(c)
    c.commit()
    c.close()
    dst.unlink()
    info = crypt.encrypt_file(plain, dst, r["key"])
    plain.unlink()
    rewrite_run_json(r["root"], r["run_id"],
                     lambda m: m["files"][kind].update(sha256=info.sha256, bytes=info.bytes_out))


def test_verify_count_mismatch(tmp_path):
    r = make_run(tmp_path)
    _swap_plain(r, "core", lambda c: c.execute("DELETE FROM audit_log"))
    rep = _v(r)
    assert not rep.ok and rep.files["core"].reason == "counts"


def test_verify_missing_required_table(tmp_path):
    r = make_run(tmp_path)
    _swap_plain(r, "core", lambda c: c.execute("DROP TABLE job_run"))
    rep = _v(r)
    assert rep.files["core"].reason == "schema"


def test_verify_newer_schema(tmp_path):
    r = make_run(tmp_path)
    _swap_plain(r, "core", lambda c: c.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}"))
    rep = _v(r)
    assert rep.files["core"].reason == "newer"


def test_verify_cleans_work_dir(tmp_path):
    r = make_run(tmp_path)
    _v(r)
    assert _no_db(r["work"])
    _v(r, key=Fernet.generate_key().decode())
    p = r["root"] / r["run_id"] / "core.pabk"
    p.write_bytes(p.read_bytes() + b"x")
    _v(r)
    assert _no_db(r["work"])


def test_verify_rejects_bad_name(tmp_path):
    r = make_run(tmp_path)
    with pytest.raises(ValueError):
        verify.verify_run(r["root"], "../x", r["key"], work_dir=r["work"])


def test_verify_only_checks_files_listed_in_run_json(tmp_path):
    r = make_run(tmp_path)
    (r["root"] / r["run_id"] / "google_token.pabk").write_bytes(b"stray")   # run.json 에 없는 파일은 보지 않는다
    rep = _v(r)
    assert rep.ok and set(rep.files) == {"core"}
    assert json.loads((r["root"] / r["run_id"] / "run.json").read_text())["files"].keys() == {"core"}
    assert sha(r["root"] / r["run_id"] / "core.pabk")

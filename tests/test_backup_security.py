from conftest import owner_client
"""백업 노출 경로·경로 조작·비밀 유출 방어선."""
import inspect
import json
import logging
import re
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backup_helpers import T0, make_live, make_run
from pa import main
from pa.backup import cli, crypt, restore, runner, store, verify
from pa.settings import Settings
from pa.web.app import create_app

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "pa" / "web"


def mk(tmp_path, **kw):
    key = Fernet.generate_key().decode()
    data = tmp_path / "data"
    make_live(data)
    s = Settings(data_dir=data, backup_key=kw.pop("backup_key", key), _env_file=None, **kw)
    return s, key, None


def test_gitignore_covers_backup_artifacts():
    lines = {ln.strip() for ln in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for want in ("data/", "*.pabk", "backups/", ".backup-work/", "pre-restore-*/", "*.restore-tmp", ".env"):
        assert want in lines, want


def test_backup_root_not_under_static(tmp_path):
    with pytest.raises(store.UnsafeLocation):
        store.check_location(WEB / "static" / "x", data_dir=REPO / "data", repo_root=REPO)
    root = Settings(_env_file=None).backup_root.resolve()
    assert not str(root).startswith(str((WEB / "static").resolve()))
    store.check_location(Settings(_env_file=None).backup_root, data_dir=Settings(_env_file=None).data_dir,
                         repo_root=REPO)


def _files(d: Path) -> set:
    return {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def test_static_mount_has_no_backup_files(tmp_path):
    before = _files(WEB / "static")
    s, _, _ = mk(tmp_path)
    assert runner.run_backup(s, T0).result == "ok"
    assert _files(WEB / "static") == before


def test_no_route_serves_backup_dir(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True,
                 data_dir=tmp_path / "data", _env_file=None)
    app = create_app(s, con, router=None)
    from starlette.staticfiles import StaticFiles
    mounts = [r for r in app.router.routes if type(r).__name__ == "Mount"]
    dirs = [Path(m.app.directory).resolve() for m in mounts if isinstance(m.app, StaticFiles)]
    assert dirs == [(WEB / "static").resolve()]
    for f in WEB.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        for bad in ("FileResponse", "backup_root", ".pabk", "backups"):
            assert bad not in src, (f.name, bad)


def test_http_probes_cannot_reach_backups(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True,
                 data_dir=tmp_path / "data", _env_file=None)
    run = s.backup_root / "20261006T180000Z"
    run.mkdir(parents=True)
    (run / "run.json").write_text('{"key_fp": "SECRETFP", "sha256": "SECRETHASH"}')
    (run / "core.pabk").write_bytes(b"PABK1\nSECRETCIPHER")
    c = owner_client(create_app(s, con, router=None))
    probes = ["/static/../data/backups/20261006T180000Z/run.json", "/static/%2e%2e/data/backups",
              "/data/backups/", "/backups", "/backups/20261006T180000Z/run.json",
              "/settings/health/../../data/backups/20261006T180000Z/run.json",
              "/static/..%2fdata%2fbackups%2f20261006T180000Z%2frun.json", "/static/20261006T180000Z/run.json"]
    for u in probes:
        r = c.get(u)
        assert r.status_code in (400, 401, 403, 404, 405, 307, 308), (u, r.status_code)
        assert "SECRETFP" not in r.text and "SECRETHASH" not in r.text and "SECRETCIPHER" not in r.text, u


NAMES = ["..\\data", "20261006T180000Z\\..\\..\\x", "C:\\Users\\x", "D:20261006T180000Z", "\\\\server\\share\\x",
         "//server/share", "20261006T180000Z\x00.x", "\u2024\u2024", "\uff0e\uff0e/x", "20261006T180000Z/", "./20261006T180000Z",
         "20261006T180000Z ", "２０２６１００６T180000Z"]


@pytest.mark.parametrize("name", NAMES)
def test_restore_name_traversal_variants(tmp_path, name):
    r = make_run(tmp_path)
    with pytest.raises(ValueError):
        store.resolve_run(r["root"], name)
    with pytest.raises(ValueError):
        verify.verify_run(r["root"], name, r["key"], work_dir=r["work"])
    with pytest.raises(ValueError):
        restore.restore_to(r["root"], name, r["key"], tmp_path / "o", work_dir=r["work"], data_dir=r["data"])
    with pytest.raises(ValueError):
        restore.replace_live(r["root"], name, r["key"], r["data"], now=T0, work_dir=r["work"],
                             is_running=lambda: False)
    assert not (tmp_path / "o").exists()


def test_backup_never_contains_env_or_keys(tmp_path):
    s, key, _ = mk(tmp_path)
    (s.data_dir / ".env").write_text("PA_SESSION_SECRET=SENTINEL-ENV")
    res = runner.run_backup(s, T0)
    assert res.result == "ok"
    run = s.backup_root / res.run_id
    for p in run.iterdir():
        raw = p.read_bytes()
        for secret in (b"SENTINEL-ENV", key.encode()):
            assert secret not in raw, p.name
        if p.suffix == ".pabk":
            plain = tmp_path / ("plain-" + p.name)
            crypt.decrypt_file(p, plain, key)
            data = plain.read_bytes()
            plain.unlink()
            for secret in (b"SENTINEL-ENV", key.encode()):
                assert secret not in data, (p.name, secret)


def test_logs_have_no_paths_or_keys(tmp_path, monkeypatch, caplog):
    s, key, _ = mk(tmp_path)
    secret_path = str(tmp_path)

    def full(src, dst, k):
        raise OSError(28, "No space left on device", secret_path)
    with caplog.at_level(logging.DEBUG):
        monkeypatch.setattr(crypt, "encrypt_file", full)
        runner.run_backup(s, T0)                                  # 디스크 가득
        monkeypatch.undo()
        monkeypatch.setattr(verify, "verify_run", lambda *a, **k: verify.VerifyReport(False, {}))
        runner.run_backup(s, T0 + __import__("datetime").timedelta(hours=1))    # 검증 실패
        monkeypatch.undo()
        runner.run_backup(Settings(data_dir=s.data_dir, backup_key="not-a-key", _env_file=None), T0)   # 잘못된 키
    text = caplog.text
    for secret in (secret_path, key, "not-a-key"):
        assert secret not in text, secret


def test_run_json_plain_fields_only(tmp_path):
    s, _, _ = mk(tmp_path)
    res = runner.run_backup(s, T0)
    meta = json.loads((s.backup_root / res.run_id / "run.json").read_text())
    assert set(meta) == {"format", "run_id", "created_at", "key_fp", "files"}
    for v in meta["files"].values():
        assert set(v) == {"bytes", "sha256"}


def test_manifest_is_encrypted(tmp_path):
    s, key, _ = mk(tmp_path)
    res = runner.run_backup(s, T0)
    raw = (s.backup_root / res.run_id / "manifest.pabk").read_bytes()
    assert b"audit_log" not in raw and b"tables" not in raw


def test_db_plaintext_never_in_backup_root_or_work_dir(tmp_path):
    s, key, _ = mk(tmp_path)
    runner.run_backup(s, T0)
    for p in list(s.backup_root.rglob("*")) + list(s.backup_work_dir.rglob("*")):
        if p.is_file():
            assert not p.read_bytes().startswith(b"SQLite format 3"), p.name


def test_unsafe_location_warning_helper(tmp_path, caplog):
    s_ok, _, _ = mk(tmp_path)
    assert runner.unsafe_location_warning(s_ok) is False
    s_bad = Settings(data_dir=s_ok.data_dir, backup_key=s_ok.backup_key, backup_dir=s_ok.data_dir, _env_file=None)
    with caplog.at_level(logging.WARNING):
        assert runner.unsafe_location_warning(s_bad) is True
    assert "백업 위치가 안전하지 않아 백업을 끕니다" in caplog.text and str(tmp_path) not in caplog.text
    no_key = Settings(data_dir=s_ok.data_dir, backup_dir=s_ok.data_dir, _env_file=None)
    assert runner.unsafe_location_warning(no_key) is False


def test_build_calls_location_check():
    assert "unsafe_location_warning" in inspect.getsource(main.build)


def test_no_shutil_copy_of_live_db_in_backup_package():
    for f in (REPO / "pa" / "backup").glob("*.py"):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"shutil\.copy\w*\(([^)]*)\)", src):
            assert "core_db_path" not in m.group(1), f.name
    # store.copy_run 은 .pabk 만 복사한다
    assert "shutil.copyfile(src / name, partial / name)" in (REPO / "pa" / "backup" / "store.py").read_text(encoding="utf-8")

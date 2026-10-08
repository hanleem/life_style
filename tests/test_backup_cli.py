import getpass
import io
import json
import re
import sqlite3
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

from backup_helpers import T0, make_live
from pa.backup import cli, restore, store
from pa.settings import Settings


def mk(tmp_path, **kw):
    key = Fernet.generate_key().decode()
    data = tmp_path / "data"
    make_live(data)
    s = Settings(data_dir=data, backup_key=kw.pop("backup_key", key), _env_file=None, **kw)
    return s, key, None


def run(argv, s, now=T0, stdin=""):
    out = io.StringIO()
    code = cli.main(argv, settings=s, now=now, stdin=io.StringIO(stdin), stdout=out)
    return code, out.getvalue()


def first_run(s):
    code, out = run(["run"], s)
    assert code == 0, out
    return store.list_runs(s.backup_root)[0].run_id


def test_genkey_prints_valid_key_and_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    s, _, _ = mk(tmp_path)
    before = set(tmp_path.iterdir())
    code, out = run(["genkey"], s)
    assert code == 0
    first = out.splitlines()[0].strip()
    Fernet(first.encode())
    assert "비밀번호 관리자" in out and "PA_BACKUP_KEY" in out
    assert set(tmp_path.iterdir()) == before


def test_run_then_list_then_verify(tmp_path):
    s, key, pkey = mk(tmp_path)
    code, out = run(["run"], s)
    assert code == 0 and "백업 완료" in out and "MB" in out
    rid = store.list_runs(s.backup_root)[0].run_id
    assert rid in out
    code, out = run(["list"], s)
    assert code == 0 and rid in out and "core" in out and "10-07" in out
    code, out = run(["verify", rid], s)
    assert code == 0 and "확인 완료" in out
    # 자동 실행 기록은 남기되 mark_run 은 하지 않는다
    c = sqlite3.connect(s.core_db_path)
    assert c.execute("SELECT COUNT(*) FROM job_run WHERE job_id='backup'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE event='backup.run'").fetchone()[0] == 1
    c.close()


def test_run_without_key_reports(tmp_path):
    s, _, _ = mk(tmp_path, backup_key="")
    code, out = run(["run"], s)
    assert code == 1 and "백업 키" in out


def test_list_empty(tmp_path):
    s, _, _ = mk(tmp_path)
    code, out = run(["list"], s)
    assert code == 0 and "백업이 없습니다" in out


def test_verify_wrong_backup_key_exit_1(tmp_path):
    s, key, _ = mk(tmp_path)
    rid = first_run(s)
    s2 = Settings(data_dir=s.data_dir, backup_key=Fernet.generate_key().decode(), _env_file=None)
    code, out = run(["verify", rid], s2)
    assert code == 1 and "백업 키" in out


def test_keycheck_never_prints_key_values(tmp_path):
    s, key, pkey = mk(tmp_path)
    first_run(s)
    code, out = run(["keycheck"], s)
    assert code == 0
    assert key not in out
    from pa.backup.crypt import key_fingerprint
    assert key_fingerprint(key) in out and "설정됨" in out and "일치" in out


def test_keycheck_missing_keys(tmp_path):
    s, _, _ = mk(tmp_path, backup_key="")
    code, out = run(["keycheck"], s)
    assert out.count("없음") >= 1


def test_restore_to_new_dir(tmp_path):
    s, key, _ = mk(tmp_path)
    rid = first_run(s)
    to = tmp_path / "restored"
    code, out = run(["restore", rid, "--to", str(to)], s)
    assert code == 0 and (to / "core.db").exists()
    assert "--replace-live" in out
    c = sqlite3.connect(s.core_db_path)
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE event='backup.restore'").fetchone()[0] == 1
    c.close()


def test_restore_only_core(tmp_path):
    s, _, _ = mk(tmp_path)
    rid = first_run(s)
    to = tmp_path / "r"
    code, _ = run(["restore", rid, "--to", str(to), "--only", "core"], s)
    assert code == 0 and sorted(p.name for p in to.iterdir()) == ["core.db"]


def test_restore_nonempty_target_refused(tmp_path):
    s, _, _ = mk(tmp_path)
    rid = first_run(s)
    to = tmp_path / "r"
    to.mkdir()
    (to / "x").write_text("x")
    code, out = run(["restore", rid, "--to", str(to)], s)
    assert code == 1 and "비어 있지 않" in out


def test_restore_invalid_name_exit_code(tmp_path):
    s, _, _ = mk(tmp_path)
    code, out = run(["restore", "../../x", "--to", str(tmp_path / "d")], s)
    assert code == 2 and "백업 이름 형식이 아닙니다" in out


def test_restore_requires_target_mode(tmp_path):
    s, _, _ = mk(tmp_path)
    rid = first_run(s)
    code, out = run(["restore", rid], s)
    assert code == 2


def _live_snapshot(s):
    return {n: (s.data_dir / n).read_bytes() for n in ("core.db",)}


def test_replace_live_requires_typed_confirmation(tmp_path, monkeypatch):
    s, key, _ = mk(tmp_path)
    rid = first_run(s)
    monkeypatch.setattr(restore, "server_running", lambda port=8010: False)
    # 백업 이후 라이브에 기록 추가
    c = sqlite3.connect(s.core_db_path)
    c.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES('late','x','t','e','{}')")
    c.commit()
    c.close()
    before = _live_snapshot(s)
    code, out = run(["restore", rid, "--replace-live"], s, stdin="y\n")
    assert code == 1 and "취소" in out
    assert _live_snapshot(s) == before
    code, out = run(["restore", rid, "--replace-live"], s, stdin="복원\n", now=T0 + timedelta(hours=2))
    assert code == 0
    c = sqlite3.connect(s.core_db_path)
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE id='late'").fetchone()[0] == 0
    rows = c.execute("SELECT detail_json FROM audit_log WHERE event='backup.restore'").fetchall()
    assert len(rows) == 1
    d = json.loads(rows[0][0])
    assert d["mode"] == "replace_live" and d["run_id"] == rid and sorted(d["only"]) == ["core"]
    c.close()
    assert list(s.data_dir.glob("pre-restore-*"))
    assert "pre-restore-" in out


def test_replace_live_refuses_running_server(tmp_path, monkeypatch):
    s, _, _ = mk(tmp_path)
    rid = first_run(s)
    monkeypatch.setattr(restore, "server_running", lambda port=8010: True)
    before = _live_snapshot(s)
    code, out = run(["restore", rid, "--replace-live"], s, stdin="복원\n")
    assert code == 2 and "서버" in out
    assert _live_snapshot(s) == before
    assert "입력" not in out


def test_replace_live_warns_resurrection(tmp_path, monkeypatch):
    s, _, _ = mk(tmp_path)
    rid = first_run(s)
    monkeypatch.setattr(restore, "server_running", lambda port=8010: False)
    code, out = run(["restore", rid, "--replace-live"], s, stdin="n\n")
    assert "되살아납니다" in out and "Ctrl+C" in out


def test_ask_key_uses_getpass(tmp_path, monkeypatch):
    s, key, _ = mk(tmp_path)
    rid = first_run(s)
    s2 = Settings(data_dir=s.data_dir, backup_key="", _env_file=None)
    calls = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": calls.append(prompt) or key)
    code, out = run(["restore", rid, "--to", str(tmp_path / "r"), "--ask-key"], s2)
    assert code == 0 and calls and key not in out


def test_no_key_and_no_ask_key_refused(tmp_path):
    s, key, _ = mk(tmp_path)
    rid = first_run(s)
    s2 = Settings(data_dir=s.data_dir, backup_key="", _env_file=None)
    code, out = run(["restore", rid, "--to", str(tmp_path / "r")], s2)
    assert code == 2 and "--ask-key" in out


def test_cli_output_has_only_core_counts(tmp_path):
    s, _, _ = mk(tmp_path)
    rid = first_run(s)
    code, out = run(["verify", rid, "--details"], s)
    assert code == 0 and "audit_log" in out
    for bad in ("family_channel", "journal_entry", "mail_thread"):
        assert bad not in out


def test_init_mirror(tmp_path):
    s, _, _ = mk(tmp_path)
    m = tmp_path / "usb"
    m.mkdir()
    code, out = run(["init-mirror", str(m)], s)
    assert code == 0 and store.mirror_ready(m) and "PA_BACKUP_MIRROR_DIR" in out


def test_init_mirror_refuses_nonempty_and_unsafe(tmp_path):
    s, _, _ = mk(tmp_path)
    m = tmp_path / "usb"
    m.mkdir()
    (m / "f").write_text("x")
    code, out = run(["init-mirror", str(m)], s)
    assert code == 1
    code, out = run(["init-mirror", str(s.data_dir)], s)
    assert code == 1


def test_list_shows_mirror_column(tmp_path):
    m = tmp_path / "usb"
    m.mkdir()
    store.init_mirror(m)
    s, _, _ = mk(tmp_path, backup_mirror_dir=m)
    first_run(s)
    code, out = run(["list"], s)
    assert "미러: 있음" in out


def test_main_module_entrypoint_exists():
    import importlib.util
    assert importlib.util.find_spec("pa.backup.__main__") is not None


def test_cli_source_has_no_key_printing():
    src = open(cli.__file__, encoding="utf-8").read()
    assert not re.search(r"print\([^)]*backup_key", src)


def test_docs_commands_exist():
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    sub = next(a for a in cli._parser()._actions if getattr(a, "choices", None) and "genkey" in a.choices)
    names = set(sub.choices)
    used = set()
    for doc in ("README.md",):
        used |= set(re.findall(r"python -m pa\.backup (\w[\w-]*)", (repo / doc).read_text(encoding="utf-8")))
    assert used, "docs must mention the backup CLI"
    assert used <= names, used - names
    assert {"genkey", "keycheck", "run", "verify", "restore", "init-mirror", "list"} <= used

"""백업 테스트 공용 헬퍼: 실제 core DB 를 만들고 K0~K2 부품으로 실행 폴더를 만든다."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from pa.backup import crypt, store
from pa.backup.snapshot import take_snapshot
from pa.db import connect, init_db

T0 = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)


def make_live(data_dir: Path, *, core_rows=3):
    data_dir.mkdir(parents=True, exist_ok=True)
    c = connect(data_dir / "core.db")
    init_db(c)
    for i in range(core_rows):
        c.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES(?,?,?,?,?)",
                  (f"a{i}", "2026-10-06T00:00:00+00:00", "t", "e", "{}"))
    c.close()


def make_run(tmp_path: Path, key: str | None = None, now=T0):
    key = key or Fernet.generate_key().decode()
    data = tmp_path / "data"
    make_live(data)
    root = tmp_path / "backups"
    work = tmp_path / "work"
    rid = store.new_run_id(now, root)
    files, manifest = {}, {}
    with store.RunWriter(root, rid) as w:
        for kind, src in (("core", data / "core.db"),):
            snap = take_snapshot(src, work, kind)
            info = crypt.encrypt_file(snap.path, w.path_for(store.DB_FILES[kind]), key)
            snap.path.unlink()
            files[kind] = {"bytes": info.bytes_out, "sha256": info.sha256}
            manifest[kind] = {"user_version": snap.user_version, "tables": snap.tables}
        w.path_for(store.MANIFEST).write_bytes(
            crypt.encrypt_bytes(json.dumps({"files": manifest}).encode(), key))
        w.commit({"format": 1, "run_id": rid, "created_at": now.isoformat(),
                  "key_fp": crypt.key_fingerprint(key), "files": files})
    return dict(root=root, run_id=rid, key=key, data=data, work=work,
                manifest=manifest)


def rewrite_run_json(root: Path, rid: str, fn):
    p = root / rid / "run.json"
    meta = json.loads(p.read_text())
    fn(meta)
    p.write_text(json.dumps(meta))


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

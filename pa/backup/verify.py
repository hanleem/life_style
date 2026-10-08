"""백업 실행 검증: 해시 -> 복호화 -> integrity_check -> user_version -> 필수 표 -> 표별 행 수."""
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pa.backup import crypt, store
from pa.backup.snapshot import inspect_db
from pa.db import SCHEMA_VERSION

REQUIRED_TABLES = {
    "core": {"audit_log", "job_run", "preference", "draft", "action"},
}
_MAX_VERSION = {"core": SCHEMA_VERSION}


@dataclass
class FileCheck:
    ok: bool
    reason: str = ""
    user_version: int | None = None
    tables: dict[str, int] = field(default_factory=dict)


@dataclass
class VerifyReport:
    ok: bool
    files: dict[str, FileCheck]
    key_check: str = "skipped"


def _rm_db(p: Path) -> None:
    for s in ("", "-journal", "-wal", "-shm"):
        try:
            Path(str(p) + s).unlink()
        except OSError:
            pass


def _check_file(kind: str, src: Path, meta, key: str, work_dir: Path, manifest: dict | None) -> tuple[FileCheck, Path | None]:
    if store.sha256_file(src) != meta.sha256:
        return FileCheck(False, "hash"), None
    work_dir.mkdir(parents=True, exist_ok=True)
    plain = work_dir / f"verify-{kind}-{uuid.uuid4().hex}.db"
    try:
        try:
            crypt.decrypt_file(src, plain, key)
        except crypt.BackupKeyError:
            return FileCheck(False, "key"), None
        except crypt.BundleCorrupt:
            return FileCheck(False, "corrupt"), None
        if kind not in _MAX_VERSION:   # google_token: DB 가 아님 — 복호화만 확인
            return FileCheck(True), plain
        try:
            con = sqlite3.connect(str(plain))
            try:
                integ = con.execute("PRAGMA integrity_check").fetchall()
            finally:
                con.close()
            if integ != [("ok",)]:
                return FileCheck(False, "integrity"), plain
            ver, tables = inspect_db(plain)
        except sqlite3.Error:
            return FileCheck(False, "integrity"), plain
        if ver > _MAX_VERSION[kind]:
            return FileCheck(False, "newer", ver, tables), plain
        if not REQUIRED_TABLES[kind] <= set(tables):
            return FileCheck(False, "schema", ver, tables), plain
        want = (manifest or {}).get("files", {}).get(kind)
        if want is None or want.get("tables") != tables or want.get("user_version") != ver:
            return FileCheck(False, "counts", ver, tables), plain
        return FileCheck(True, "", ver, tables), plain
    finally:
        _rm_db(plain)


def verify_run(root: Path, run_id: str, key: str, *, work_dir: Path) -> VerifyReport:
    d = store.resolve_run(root, run_id)
    info = store.parse_run(d)
    if info is None:
        raise FileNotFoundError(run_id)
    kinds = [k for k in info.files if k in store.DB_FILES]
    if info.key_fp != crypt.key_fingerprint(key):
        return VerifyReport(False, {k: FileCheck(False, "key") for k in kinds})
    manifest = None
    try:
        manifest = json.loads(crypt.decrypt_bytes((d / store.MANIFEST).read_bytes(), key))
    except crypt.BackupKeyError:
        return VerifyReport(False, {k: FileCheck(False, "key") for k in kinds})
    except (crypt.BundleCorrupt, OSError, ValueError):
        manifest = None   # 매니페스트가 없거나 깨짐 -> DB 파일마다 counts 불일치로 실패한다
    out: dict[str, FileCheck] = {}
    for k in kinds:
        chk, _ = _check_file(k, d / store.DB_FILES[k], info.files[k], key, Path(work_dir), manifest)
        out[k] = chk
    key_check = "skipped"
    return VerifyReport(all(c.ok for c in out.values()) and bool(out), out, key_check)

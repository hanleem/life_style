"""일관된 온라인 스냅샷: 별도 연결 + VACUUM INTO (읽기 트랜잭션 1개, WAL 쓰기를 막지 않음).

라이브 .db 파일을 복사하지 않는다. 사본에는 살아 있는 행만 들어가 삭제된 행·덮어쓴 암호문 조각이 없다.
"""
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MIN_SQLITE = (3, 27, 0)
BUSY_TIMEOUT_S = 5.0


class SnapshotError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Snapshot:
    name: str
    path: Path
    bytes: int
    user_version: int
    tables: dict[str, int] = field(default_factory=dict)


def sqlite_supported() -> bool:
    return tuple(sqlite3.sqlite_version_info) >= MIN_SQLITE


def inspect_db(path: Path) -> tuple[int, dict[str, int]]:
    con = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S)
    try:
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables = {}
        for n in names:
            q = '"' + n.replace('"', '""') + '"'
            tables[n] = con.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
        return ver, tables
    finally:
        con.close()


def _remove_db_files(out: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            Path(str(out) + suffix).unlink()
        except OSError:
            pass


def _code(e: Exception) -> str:
    if isinstance(e, sqlite3.OperationalError) and "locked" in str(e).lower():
        return "busy"
    if isinstance(e, sqlite3.DatabaseError) and "malformed" in str(e).lower():
        return "integrity"
    if isinstance(e, OSError) or isinstance(e, sqlite3.OperationalError):
        return "io"
    return "unexpected"


def take_snapshot(src: Path, work_dir: Path, name: str) -> Snapshot:
    src = Path(src)
    if not sqlite_supported():
        raise SnapshotError("sqlite_old")
    if not src.exists():
        raise SnapshotError("absent")
    work_dir = Path(work_dir)
    out = None
    con = None
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(work_dir, 0o700)
        out = work_dir / f"{name}-{uuid.uuid4().hex}.db"
        # 읽기 전용 URI 연결: 라이브 DB 를 바꿀 수 없다(query_only 는 VACUUM INTO 까지 막으므로 쓰지 않는다).
        con = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S,
                              isolation_level=None)
        con.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
        con.execute("VACUUM INTO ?", (str(out),))
        con.close()
        con = None
        chk = sqlite3.connect(str(out))
        try:
            if chk.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise SnapshotError("integrity")
        finally:
            chk.close()
        ver, tables = inspect_db(out)
        fd = os.open(str(out), os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return Snapshot(name, out, out.stat().st_size, ver, tables)
    except SnapshotError:
        if out is not None:
            _remove_db_files(out)
        raise
    except Exception as e:
        if out is not None:
            _remove_db_files(out)
        err = SnapshotError(_code(e))
        raise err from e
    finally:
        if con is not None:
            con.close()

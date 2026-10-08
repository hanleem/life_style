import functools
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from pa.clock import utcnow_iso

SCHEMA_VERSION = 1   # 가족 비서 스키마 v1
_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def get_cursor(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM sync_cursor WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_cursor(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO sync_cursor(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, utcnow_iso()))


@contextmanager
def atomic(con: sqlite3.Connection):
    """One atomic unit: a SAVEPOINT (outermost one opens/commits the transaction,
    nested ones roll back only themselves), so callers already in a transaction work."""
    name = "sp_" + uuid.uuid4().hex
    con.execute(f"SAVEPOINT {name}")
    try:
        yield con
    except BaseException:
        con.execute(f"ROLLBACK TO {name}")
        con.execute(f"RELEASE {name}")
        raise
    else:
        con.execute(f"RELEASE {name}")


def transactional(fn):
    """Run fn(con, ...) as one atomic unit (see atomic)."""
    @functools.wraps(fn)
    def wrapper(con, *a, **kw):
        with atomic(con):
            return fn(con, *a, **kw)
    return wrapper

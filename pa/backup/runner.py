"""백업 실행 본체. run_backup 은 스레드에서 돌고 core.db 연결을 받지 않는다(기록은 record_result 가 메인 루프에서)."""
import json
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

import pa
from pa.backup import crypt, store, verify as _verify
from pa.backup.snapshot import SnapshotError, take_snapshot
from pa.db import atomic
from pa.ids import new_id

log = logging.getLogger(__name__)

REASONS = ("no_key", "bad_key", "unsafe_location", "locked", "low_disk", "too_large", "sqlite_old", "busy",
           "integrity", "io", "verify", "unexpected")
_MB = 1024 * 1024
_MARGIN = 200 * _MB


@dataclass
class RunResult:
    run_id: str | None = None
    result: str = "failed"          # ok | partial | failed | skipped
    reason: str = ""
    files: int = 0
    bytes: int = 0
    ms: int = 0
    mirror: str = "off"             # ok | missing | off | failed
    purged: int = 0


def _repo_root() -> Path:
    return Path(pa.__file__).resolve().parent.parent


def unsafe_location_warning(settings) -> bool:
    """시작 시 점검: 백업 키가 있는데 위치가 안전하지 않으면 경고 로그(경로 없이)를 남기고 True. 서버는 그대로 시작하며
    실제 차단은 run_backup 의 unsafe_location 이 한다."""
    if not settings.backup_key:
        return False
    try:
        for loc in (settings.backup_root, settings.backup_mirror_dir):
            if loc is not None:
                store.check_location(Path(loc), data_dir=settings.data_dir, repo_root=_repo_root())
    except store.UnsafeLocation:
        log.warning("백업 위치가 안전하지 않아 백업을 끕니다")
        return True
    return False


def _clear_dir(d: Path) -> None:
    if not d.is_dir():
        return
    for p in d.iterdir():
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
        except OSError:
            pass


def _rm_snapshot(path: Path) -> None:
    for s in ("", "-journal", "-wal", "-shm"):
        try:
            Path(str(path) + s).unlink()
        except OSError:
            pass


def _src_size(p: Path) -> int:
    n = 0
    for s in ("", "-wal"):
        try:
            n += Path(str(p) + s).stat().st_size
        except OSError:
            pass
    return n


def _targets(settings):
    t = [("core", settings.core_db_path)]
    if settings.backup_include_google_token:
        t.append(("google_token", settings.google_token_path))
    return t


def _retention(root: Path, now: datetime) -> int:
    plan = store.plan_retention(store.list_runs(root), now)
    return store.apply_retention(root, plan)


def run_backup(settings, now: datetime) -> RunResult:
    t0 = time.monotonic()
    res = RunResult()
    stage = ["start"]
    try:
        _run(settings, now, res, stage)
    except Exception:
        log.error("backup unexpected failure stage=%s", stage[0])   # 경로·내용 없이 단계 이름만
        res.result, res.reason = "failed", "unexpected"
    res.ms = int((time.monotonic() - t0) * 1000)
    return res


def _run(settings, now: datetime, res: RunResult, stage: list) -> None:
    key = settings.backup_key
    if not key:
        res.result, res.reason = "skipped", "no_key"
        return
    try:
        Fernet(key.encode())
    except Exception:
        res.result, res.reason = "failed", "bad_key"
        return
    root = Path(settings.backup_root)
    work = Path(settings.backup_work_dir)
    mirror = Path(settings.backup_mirror_dir) if settings.backup_mirror_dir else None
    stage[0] = "location"
    try:
        for loc in (root, mirror):
            if loc is not None:
                store.check_location(loc, data_dir=settings.data_dir, repo_root=_repo_root())
    except store.UnsafeLocation:
        res.result, res.reason = "failed", "unsafe_location"
        return
    stage[0] = "lock"
    lock = store.acquire_lock(root, now)
    if lock is None:
        res.result, res.reason = "skipped", "locked"
        return
    try:
        _locked(settings, now, res, stage, key, root, work, mirror)
    finally:
        lock.release()
        _clear_dir(work)


def _locked(settings, now, res, stage, key, root: Path, work: Path, mirror: Path | None) -> None:
    stage[0] = "cleanup"
    store.cleanup_stale(root, now)
    _clear_dir(work)    # 이전 비정상 종료가 남긴 평문 사본
    store.purge_pre_restore(settings.data_dir, now)
    work.mkdir(parents=True, exist_ok=True)
    store.harden(work)

    stage[0] = "write"
    ok_kinds: list[str] = []
    fail_reasons: list[str] = []
    files_meta: dict[str, dict] = {}
    manifest: dict[str, dict] = {}
    total = 0
    run_id = store.new_run_id(now, root)
    committed = False
    try:
        with store.RunWriter(root, run_id) as w:
            for kind, src in _targets(settings):
                if not src.exists():
                    if kind == "core":
                        fail_reasons.append("io")
                    continue   # google_token 이 없는 PC: 실패가 아니라 건너뜀
                size = _src_size(src)
                if size > settings.backup_max_mb * _MB:
                    fail_reasons.append("too_large")
                    continue
                if (shutil.disk_usage(work).free < 2 * size + _MARGIN
                        or shutil.disk_usage(root).free < int(1.1 * size) + _MARGIN):
                    fail_reasons.append("low_disk")
                    continue
                snap_path = None
                try:
                    if kind == "google_token":
                        enc_src, uv, tables = src, 0, {}
                    else:
                        snap = take_snapshot(src, work, kind)
                        snap_path, enc_src, uv, tables = snap.path, snap.path, snap.user_version, snap.tables
                    info = crypt.encrypt_file(enc_src, w.path_for(store.DB_FILES[kind]), key)
                except SnapshotError as e:
                    if e.code == "absent":
                        continue
                    fail_reasons.append(e.code if e.code in REASONS else "unexpected")
                    continue
                except OSError:
                    fail_reasons.append("io")
                    continue
                finally:
                    if snap_path is not None:
                        _rm_snapshot(snap_path)
                files_meta[kind] = {"bytes": info.bytes_out, "sha256": info.sha256}
                manifest[kind] = {"user_version": uv, "tables": tables}
                ok_kinds.append(kind)
                total += info.bytes_out
            if not ok_kinds:
                raise _NothingWritten
            stage[0] = "commit"
            mp = w.path_for(store.MANIFEST)
            mp.write_bytes(crypt.encrypt_bytes(json.dumps({"files": manifest}).encode(), key))
            store.harden(mp)
            w.commit({"format": 1, "run_id": run_id, "created_at": now.astimezone(timezone.utc).isoformat(),
                      "key_fp": crypt.key_fingerprint(key), "files": files_meta})
            committed = True
    except _NothingWritten:
        pass
    if not committed:
        res.result = "failed"
        res.reason = fail_reasons[0] if fail_reasons else "unexpected"
        _post_retention(settings, now, res, root, mirror, copy=False)
        return

    res.run_id, res.files, res.bytes = run_id, len(ok_kinds), total
    stage[0] = "verify"
    rep = _verify.verify_run(root, run_id, key, work_dir=work)
    if not rep.ok:
        shutil.rmtree(root / run_id, ignore_errors=True)   # 나쁜 백업을 남기지 않는다(이전 백업은 그대로)
        res.run_id, res.files, res.bytes = None, 0, 0
        res.result, res.reason = "failed", "verify"
        _post_retention(settings, now, res, root, mirror, copy=False)
        return
    res.result = "ok" if not fail_reasons else "partial"
    res.reason = fail_reasons[0] if fail_reasons else ""
    _post_retention(settings, now, res, root, mirror, copy=True)


class _NothingWritten(Exception):
    pass


def _post_retention(settings, now, res: RunResult, root: Path, mirror: Path | None, *, copy: bool) -> None:
    """미러 정리 -> (복사) -> 1차 정리. 14일 지난 복원 전 사본 정리는 성공 여부와 무관하게 적용한다."""
    if mirror is None:
        res.mirror = "off"
    elif not store.mirror_ready(mirror):
        res.mirror = "missing"
    else:
        try:
            _retention(mirror, now)          # 14일 지난 항목을 복사 전에 먼저 정리
            if copy and res.run_id:
                store.copy_run(root, res.run_id, mirror)
            res.mirror = "ok"
        except Exception:
            log.error("backup mirror step failed")
            res.mirror = "failed"
    res.purged = _retention(root, now)       # 예외는 run_backup 이 failed/unexpected 로 처리한다


# ---- 결과 기록(메인 스레드 전용) ----

def _get(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM preference WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set(con, key: str, value: str, now_iso: str) -> None:
    con.execute("INSERT INTO preference(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now_iso))


def _del(con, key: str) -> None:
    con.execute("DELETE FROM preference WHERE key=?", (key,))


def note_attempt(con, now: datetime) -> int:
    """오늘(KST) 시도 횟수를 1 올리고 시각을 기록한다. 날짜가 바뀌면 1부터."""
    day = now.astimezone(store.KST).date().isoformat()
    iso = now.astimezone(timezone.utc).isoformat()
    cur = _get(con, "backup.attempts") or ""
    d, _, n = cur.partition(":")
    count = (int(n) + 1) if d == day and n.isdigit() else 1
    with atomic(con):
        _set(con, "backup.attempts", f"{day}:{count}", iso)
        _set(con, "backup.last_attempt_at", iso, iso)
    return count


def record_result(con, res: RunResult, now: datetime, *, attempt: bool = True) -> None:
    iso = now.astimezone(timezone.utc).isoformat()
    with atomic(con):
        con.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES(?,?,?,?,?)",
                    (new_id(), iso, "pa", "backup.run", json.dumps({
                        "run_id": res.run_id, "result": res.result, "reason": res.reason, "files": res.files,
                        "bytes": res.bytes, "ms": res.ms, "mirror": res.mirror, "purged": res.purged})))
        if attempt:
            note_attempt(con, now)
        if res.result == "skipped" and res.reason == "locked":
            return    # 다른 실행이 진행 중 — 상태를 덮어쓰지 않는다
        _set(con, "backup.last_result", res.result, iso)
        _set(con, "backup.last_reason", res.reason, iso)
        if res.result in ("ok", "partial"):
            _set(con, "backup.last_ok_at", iso, iso)
            _set(con, "backup.last_bytes", str(res.bytes), iso)
            _set(con, "backup.ever_ok", "1", iso)
        if res.result != "skipped":
            _set(con, "backup.mirror_state", res.mirror, iso)
            if res.mirror == "missing":
                if _get(con, "backup.mirror_missing_since") is None:
                    _set(con, "backup.mirror_missing_since", iso, iso)
            elif res.mirror in ("ok", "off"):
                _del(con, "backup.mirror_missing_since")


@dataclass
class BackupStatus:
    enabled: bool
    last_ok_at: datetime | None = None
    last_bytes: int | None = None
    last_result: str | None = None
    last_reason: str | None = None
    mirror_state: str | None = None
    mirror_missing_since: datetime | None = None


def _dt(v: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def status(con, *, enabled: bool) -> BackupStatus:
    lb = _get(con, "backup.last_bytes")
    return BackupStatus(
        enabled=enabled,
        last_ok_at=_dt(_get(con, "backup.last_ok_at")),
        last_bytes=int(lb) if lb and lb.isdigit() else None,
        last_result=_get(con, "backup.last_result"),
        last_reason=_get(con, "backup.last_reason"),
        mirror_state=_get(con, "backup.mirror_state"),
        mirror_missing_since=_dt(_get(con, "backup.mirror_missing_since")),
    )

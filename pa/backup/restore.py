"""복원: 기본은 새(빈) 폴더로만. 라이브 교체는 서버 정지 확인 + 검증 통과 + 기존 파일 보존 후에만."""
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pa.backup import crypt, store
from pa.backup.verify import verify_run

_TARGETS = {"core": "core.db", "google_token": "google_token.enc"}
_DB_KINDS = ("core",)


class RestoreRefused(Exception):
    def __init__(self, code: str, reasons: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.reasons = reasons


@dataclass
class RestoreResult:
    run_id: str
    files: list[str] = field(default_factory=list)
    target: Path | None = None
    pre_restore: Path | None = None


def server_running(port: int = 8010) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _is_under(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except ValueError:
        return False


def _verified_kinds(root, run_id, key, only, work_dir) -> list[str]:
    rep = verify_run(root, run_id, key, work_dir=work_dir)
    bad = {k: c.reason for k, c in rep.files.items() if k in only and not c.ok}
    if bad:
        raise RestoreRefused("verify", tuple(sorted(set(bad.values()))))
    kinds = [k for k in only if k in rep.files]
    if not kinds:
        raise RestoreRefused("missing")
    return kinds


def _rm(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def restore_to(root: Path, run_id: str, key: str, to: Path, *, only=("core",),
               work_dir: Path, data_dir: Path, mirror: Path | None = None) -> RestoreResult:
    run_dir = store.resolve_run(root, run_id)
    to = Path(to)
    tr = to.resolve()
    unsafe = [Path(data_dir).resolve(), Path(root).resolve()] + ([Path(mirror).resolve()] if mirror else [])
    if any(tr == u or _is_under(tr, u) for u in unsafe):
        raise RestoreRefused("unsafe_target")
    if to.exists() and (not to.is_dir() or any(to.iterdir())):
        raise RestoreRefused("not_empty")
    kinds = _verified_kinds(root, run_id, key, tuple(only), work_dir)
    created = not to.exists()
    to.mkdir(parents=True, exist_ok=True)
    done: list[Path] = []
    try:
        for k in kinds:
            tmp = to / f".partial-{k}-{uuid.uuid4().hex}"
            try:
                crypt.decrypt_file(run_dir / store.DB_FILES[k], tmp, key)
                os.replace(tmp, to / _TARGETS[k])
            finally:
                _rm(tmp)
            store.harden(to / _TARGETS[k])
            done.append(to / _TARGETS[k])
    except BaseException:
        for p in done:
            _rm(p)
        if created:
            try:
                to.rmdir()
            except OSError:
                pass
        raise
    return RestoreResult(run_id, [_TARGETS[k] for k in kinds], to)


def replace_live(root: Path, run_id: str, key: str, data_dir: Path, *, only=("core",),
                 now: datetime, work_dir: Path, is_running=None) -> RestoreResult:
    if (is_running or server_running)():
        raise RestoreRefused("server_running")
    run_dir = store.resolve_run(root, run_id)
    data_dir = Path(data_dir)
    kinds = _verified_kinds(root, run_id, key, tuple(only), work_dir)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pre = data_dir / f"pre-restore-{stamp}"
    tmps: dict[str, Path] = {}
    moved: list[tuple[Path, Path]] = []      # (원래 위치, pre-restore 위치)
    replaced: list[Path] = []
    try:
        # 1) 먼저 전부 복호화(라이브는 아직 그대로) — 디스크 부족 등은 여기서 드러난다
        for k in kinds:
            tmp = data_dir / (_TARGETS[k] + ".restore-tmp")
            _rm(tmp)
            tmps[k] = tmp
            crypt.decrypt_file(run_dir / store.DB_FILES[k], tmp, key)
        # 2) 라이브 파일을 -wal/-shm 과 함께 옮긴다(옛 WAL 이 복원본에 적용되면 손상됨)
        pre.mkdir(parents=True, exist_ok=False)
        store.harden(pre)
        for k in kinds:
            base = data_dir / _TARGETS[k]
            for suffix in (("", "-wal", "-shm") if k in _DB_KINDS else ("",)):
                src = Path(str(base) + suffix)
                if src.exists():
                    dst = pre / src.name
                    os.replace(src, dst)
                    moved.append((src, dst))
        # 3) 교체
        for k in kinds:
            dst = data_dir / _TARGETS[k]
            os.replace(tmps[k], dst)
            replaced.append(dst)
            store.harden(dst)
    except BaseException:
        for p in replaced:
            _rm(p)
        for src, dst in reversed(moved):
            try:
                os.replace(dst, src)
            except OSError:
                pass
        for t in tmps.values():
            _rm(t)
        try:
            pre.rmdir()
        except OSError:
            pass
        raise
    return RestoreResult(run_id, [_TARGETS[k] for k in kinds], data_dir, pre)

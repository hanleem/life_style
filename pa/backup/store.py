"""백업 저장소: 실행 폴더 레이아웃, 원자적 확정, 보존, 잠금, 미러, 위치 안전 검사."""
import hashlib
import json
import logging
import os
import re
import shutil
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
RUN_RE = re.compile(r"\A[0-9]{8}T[0-9]{6}Z\Z")   # ASCII 숫자만(전각 숫자 거부)
DB_FILES = {"core": "core.pabk", "google_token": "google_token.pabk"}
RUN_JSON = "run.json"
MANIFEST = "manifest.pabk"
MARKER = ".pa-backup-target"
LOCK = ".lock"
PRE_RESTORE_MAX_AGE_DAYS = 14
CORE_DAILY = 7
CORE_WEEKLY = 4
PARTIAL_MAX_AGE = timedelta(hours=1)
LOCK_STALE = timedelta(hours=2)
_RUN_FMT = "%Y%m%dT%H%M%SZ"


class UnsafeLocation(Exception):
    pass


@dataclass
class FileMeta:
    bytes: int
    sha256: str


@dataclass
class RunInfo:
    run_id: str
    created_at: datetime
    key_fp: str
    files: dict[str, FileMeta]
    path: Path


@dataclass
class RetentionPlan:
    delete_files: list[tuple[str, str]] = field(default_factory=list)   # (kind, run_id)
    delete_runs: list[str] = field(default_factory=list)


def harden(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)
    except OSError:
        log.warning("backup: permission hardening failed")


def _fsync_path(p: Path) -> None:
    try:
        fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_dir(p: Path) -> None:
    if os.name != "nt":
        _fsync_path(p)


def new_run_id(now: datetime, root: Path | None = None) -> str:
    t = now.astimezone(timezone.utc).replace(microsecond=0)
    while True:
        rid = t.strftime(_RUN_FMT)
        if root is None or not ((Path(root) / rid).exists() or (Path(root) / f".partial-{rid}").exists()):
            return rid
        t += timedelta(seconds=1)


def _write_json_atomic(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _rename_dir(src: Path, dst: Path) -> None:
    for attempt in range(3):
        try:
            os.rename(src, dst)
            return
        except PermissionError:   # Windows: 백신·동기화가 잡고 있을 때 (TO VERIFY)
            if attempt == 2:
                raise
            _time.sleep(0.5)


class RunWriter:
    def __init__(self, root: Path, run_id: str):
        self.root = Path(root)
        self.run_id = run_id
        self.partial = self.root / f".partial-{run_id}"
        self.final = self.root / run_id
        self._done = False

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        harden(self.root)
        self.partial.mkdir()
        harden(self.partial)
        return self

    def path_for(self, name: str) -> Path:
        return self.partial / name

    def commit(self, run_meta: dict) -> None:
        _write_json_atomic(self.partial / RUN_JSON, run_meta)
        for p in self.partial.iterdir():
            _fsync_path(p)
        _fsync_dir(self.partial)
        _rename_dir(self.partial, self.final)
        _fsync_dir(self.root)
        self._done = True

    def __exit__(self, et, ev, tb):
        if not self._done:
            shutil.rmtree(self.partial, ignore_errors=True)
        return False


def parse_run(p: Path) -> RunInfo | None:
    try:
        meta = json.loads((p / RUN_JSON).read_text(encoding="utf-8"))
        files = {k: FileMeta(int(v["bytes"]), str(v["sha256"])) for k, v in meta.get("files", {}).items()}
        return RunInfo(str(meta["run_id"]), datetime.fromisoformat(meta["created_at"]),
                       str(meta.get("key_fp", "")), files, p)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def list_runs(root: Path) -> list[RunInfo]:
    root = Path(root)
    out = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for p in entries:
        if not RUN_RE.match(p.name) or p.is_symlink() or not p.is_dir():
            continue
        info = parse_run(p)
        if info is not None and info.run_id == p.name:
            out.append(info)
    out.sort(key=lambda r: r.created_at)
    return out


def resolve_run(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not RUN_RE.fullmatch(name):
        raise ValueError("백업 이름 형식이 아닙니다")
    root = Path(root).resolve()
    p = (root / name).resolve()
    if p.parent != root:
        raise ValueError("백업 이름 형식이 아닙니다")
    if not p.is_dir():
        raise FileNotFoundError(name)
    return p


def plan_retention(runs: list[RunInfo], now: datetime) -> RetentionPlan:
    plan = RetentionPlan()
    removed: dict[str, set[str]] = {}

    def drop(kind: str, r: RunInfo) -> None:
        plan.delete_files.append((kind, r.run_id))
        removed.setdefault(r.run_id, set()).add(kind)

    for kind in ("core", "google_token"):
        have = [r for r in runs if kind in r.files]
        by_day: dict = {}
        for r in sorted(have, key=lambda x: x.created_at):
            by_day[r.created_at.astimezone(KST).date()] = r     # 날짜별 최신
        days = sorted(by_day, reverse=True)
        keep = {by_day[d].run_id for d in days[:CORE_DAILY]}
        by_week: dict = {}
        for d in sorted(by_day):
            iso = d.isocalendar()
            by_week[(iso[0], iso[1])] = by_day[d]               # 주별 최신
        for wk in sorted(by_week, reverse=True)[:CORE_WEEKLY]:
            keep.add(by_week[wk].run_id)
        for r in have:
            if r.run_id not in keep:
                drop(kind, r)
    for r in runs:
        left = set(r.files) - removed.get(r.run_id, set())
        if not left:
            plan.delete_runs.append(r.run_id)
    return plan


def apply_retention(root: Path, plan: RetentionPlan) -> int:
    root = Path(root)
    n = 0
    by_run: dict[str, list[str]] = {}
    for kind, rid in plan.delete_files:
        if kind in DB_FILES and RUN_RE.fullmatch(rid):
            by_run.setdefault(rid, []).append(kind)
    for rid, kinds in by_run.items():
        d = root / rid
        if d.is_symlink() or not d.is_dir():
            continue
        for kind in kinds:
            try:
                (d / DB_FILES[kind]).unlink()
                n += 1
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("backup: retention delete failed")
        info = parse_run(d)
        if info is not None:
            meta = json.loads((d / RUN_JSON).read_text(encoding="utf-8"))
            for kind in kinds:
                meta.get("files", {}).pop(kind, None)
            _write_json_atomic(d / RUN_JSON, meta)
    for rid in plan.delete_runs:
        d = root / rid
        if not RUN_RE.fullmatch(rid) or d.is_symlink() or not d.is_dir():
            continue
        for name in (*DB_FILES.values(), MANIFEST, RUN_JSON, RUN_JSON + ".tmp"):
            try:
                (d / name).unlink()
            except OSError:
                pass
        try:
            d.rmdir()    # 모르는 파일이 남아 있으면 폴더는 그대로 둔다
        except OSError:
            pass
    return n


def cleanup_stale(root: Path, now: datetime) -> int:
    n = 0
    try:
        entries = list(Path(root).glob(".partial-*"))
    except OSError:
        return 0
    for p in entries:
        try:
            if now.timestamp() - p.stat().st_mtime > PARTIAL_MAX_AGE.total_seconds():
                shutil.rmtree(p, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


def purge_pre_restore(data_dir: Path, now: datetime) -> int:
    n = 0
    try:
        entries = list(Path(data_dir).glob("pre-restore-*"))
    except OSError:
        return 0
    for p in entries:
        m = re.fullmatch(r"pre-restore-(\d{8}T\d{6}Z)", p.name)
        if not m or p.is_symlink() or not p.is_dir():
            continue
        try:
            t = datetime.strptime(m.group(1), _RUN_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now - t >= timedelta(days=PRE_RESTORE_MAX_AGE_DAYS):
            shutil.rmtree(p, ignore_errors=True)
            n += 1
    return n


class Lock:
    def __init__(self, path: Path):
        self.path = path

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.release()
        return False


def _try_create_lock(p: Path, now: datetime) -> bool:
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as f:
        f.write(now.astimezone(timezone.utc).isoformat().encode())
    return True


def acquire_lock(root: Path, now: datetime) -> Lock | None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    p = root / LOCK
    if _try_create_lock(p, now):
        return Lock(p)
    try:
        created = datetime.fromisoformat(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        created = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) if p.exists() else None
    if created is not None and now - created > LOCK_STALE:
        try:
            p.unlink()
        except OSError:
            return None
        if _try_create_lock(p, now):
            return Lock(p)
    return None


def mirror_ready(d: Path) -> bool:
    d = Path(d)
    return d.is_dir() and (d / MARKER).is_file()


def init_mirror(d: Path) -> None:
    d = Path(d)
    if not d.is_dir():
        raise UnsafeLocation("폴더가 없습니다")
    if (d / MARKER).exists():
        return
    if any(d.iterdir()):
        raise UnsafeLocation("비어 있는 폴더만 백업 위치로 지정할 수 있습니다")
    (d / MARKER).write_text("pa-backup-target\n", encoding="utf-8")
    harden(d)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def copy_run(root: Path, run_id: str, mirror: Path) -> None:
    src = resolve_run(root, run_id)
    mirror = Path(mirror)
    meta = json.loads((src / RUN_JSON).read_text(encoding="utf-8"))
    dst_final = mirror / run_id
    partial = mirror / f".partial-{run_id}"
    if dst_final.exists():
        return
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir()
    harden(partial)
    ok = False
    try:
        names = [DB_FILES[k] for k in meta.get("files", {}) if k in DB_FILES]
        if (src / MANIFEST).exists():
            names.append(MANIFEST)
        for name in names:
            shutil.copyfile(src / name, partial / name)
            want = meta["files"].get(next((k for k, v in DB_FILES.items() if v == name), ""), {}).get("sha256")
            want = want or sha256_file(src / name)
            if sha256_file(partial / name) != want:
                raise OSError("mirror hash mismatch")
            _fsync_path(partial / name)
            harden(partial / name)
        _write_json_atomic(partial / RUN_JSON, meta)
        _fsync_dir(partial)
        _rename_dir(partial, dst_final)
        ok = True
    finally:
        if not ok:
            shutil.rmtree(partial, ignore_errors=True)


def _is_under(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except ValueError:
        return False


def check_location(path: Path, *, data_dir: Path, repo_root: Path) -> None:
    p = Path(path).resolve()
    data = Path(data_dir).resolve()
    repo = Path(repo_root).resolve()
    if p == data:
        raise UnsafeLocation("데이터 폴더와 같습니다")
    if _is_under(p, data / ".backup-work"):
        raise UnsafeLocation("작업 폴더 안입니다")
    if _is_under(p, repo / "pa"):
        raise UnsafeLocation("프로그램 폴더 안입니다")
    if _is_under(p, repo) and not _is_under(p, data):
        raise UnsafeLocation("저장소 안(git 에 올라갈 수 있음)입니다")

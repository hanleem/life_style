"""python -m pa.backup {run, list, verify, restore, genkey, keycheck, init-mirror}. 키·평문은 출력하지 않는다."""
import argparse
import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pa.backup import copy as bcopy
from pa.backup import crypt, restore, runner, store, verify
from pa.backup.restore import RestoreRefused
from pa.backup.runner import _repo_root


class _Args(argparse.ArgumentParser):
    def error(self, message):   # SystemExit 대신 종료 코드 2 로 돌려준다
        raise _UsageError(message)


class _UsageError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    p = _Args(prog="python -m pa.backup", description="PA 백업·복원")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Args)
    sub.add_parser("run", help="지금 백업")
    sub.add_parser("list", help="백업 목록")
    v = sub.add_parser("verify", help="백업 확인")
    v.add_argument("name")
    v.add_argument("--details", action="store_true")
    v.add_argument("--ask-key", action="store_true")
    r = sub.add_parser("restore", help="복원 (기본: 새 폴더)")
    r.add_argument("name")
    r.add_argument("--to")
    r.add_argument("--only", choices=("core",))
    r.add_argument("--replace-live", action="store_true")
    r.add_argument("--ask-key", action="store_true")
    sub.add_parser("genkey", help="새 백업 키 출력")
    sub.add_parser("keycheck", help="키 설정 확인")
    m = sub.add_parser("init-mirror", help="외장 백업 위치 지정")
    m.add_argument("dir")
    return p


def _size(n: int | None) -> str:
    return "-" if n is None else f"{n / (1024 * 1024):.1f} MB"


def _kst(dt: datetime) -> str:
    return dt.astimezone(store.KST).strftime("%Y-%m-%d %H:%M")


class _Out:
    def __init__(self, f):
        self.f = f

    def __call__(self, *a):
        print(*a, file=self.f)


def _core_con(settings):
    """라이브 core.db 가 있을 때만 연다(없으면 만들지 않는다)."""
    if not Path(settings.core_db_path).exists():
        return None
    from pa.db import connect
    try:
        return connect(settings.core_db_path)
    except Exception:
        return None


def _audit(settings, now, event: str, detail: dict) -> None:
    con = _core_con(settings)
    if con is None:
        return
    try:
        from pa.ids import new_id
        con.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES(?,?,?,?,?)",
                    (new_id(), now.astimezone(timezone.utc).isoformat(), "pa", event, json.dumps(detail)))
    except Exception:
        pass
    finally:
        con.close()


def _key(args, settings, out) -> str | None:
    if getattr(args, "ask_key", False):
        return getpass.getpass("백업 키: ")
    if not settings.backup_key:
        out("백업 키(PA_BACKUP_KEY)가 없습니다. .env 에 설정하거나 --ask-key 로 입력하세요.")
        return None
    return settings.backup_key


_REFUSAL = {
    "not_empty": "대상 폴더가 비어 있지 않습니다. 새(빈) 폴더를 지정하세요.",
    "unsafe_target": "대상 폴더가 데이터·백업 폴더 안에 있습니다. 다른 위치를 지정하세요.",
    "missing": "백업에 복원할 파일이 없습니다.",
    "server_running": "PA 서버가 켜져 있습니다. 서버를 끈 뒤 다시 실행하세요.",
}
_VERIFY_TEXT = {"key": "백업 키가 이 백업과 다릅니다", "hash": "백업 파일이 손상되었습니다",
                "corrupt": "백업 파일이 손상되었습니다", "integrity": "데이터 파일 점검에 문제가 있습니다",
                "newer": "더 새로운 버전의 데이터입니다", "schema": "필요한 표가 없습니다",
                "counts": "행 수가 기록과 다릅니다"}


def _refusal_text(e: RestoreRefused) -> str:
    if e.code == "verify":
        return "복원하지 않았습니다 — " + ", ".join(_VERIFY_TEXT.get(r, r) for r in e.reasons)
    return _REFUSAL.get(e.code, "복원하지 않았습니다")


def main(argv=None, *, settings=None, now=None, stdin=None, stdout=None) -> int:
    from pa.clock import utcnow
    out = _Out(stdout or sys.stdout)
    stdin = stdin or sys.stdin
    now = now or utcnow()
    if settings is None:
        from pa.settings import Settings
        settings = Settings()
    try:
        args = _parser().parse_args(argv)
    except _UsageError as e:
        out(f"사용법 오류: {e}")
        return 2
    try:
        return _dispatch(args, settings, now, stdin, out)
    except ValueError:
        out("백업 이름 형식이 아닙니다")
        return 2
    except FileNotFoundError:
        out("백업을 찾을 수 없습니다")
        return 2
    except crypt.BackupKeyError as e:
        out(str(e))
        return 1


def _dispatch(args, settings, now, stdin, out) -> int:
    root = Path(settings.backup_root)
    cmd = args.cmd
    if cmd == "genkey":
        out(crypt.generate_key())
        out("이 값을 .env 의 PA_BACKUP_KEY 에 넣고, 비밀번호 관리자에도 보관하세요.")
        out("이 창의 내용은 다른 사람에게 보내지 마세요.")
        return 0
    if cmd == "run":
        res = runner.run_backup(settings, now)
        con = _core_con(settings)
        if con is not None:
            try:
                runner.record_result(con, res, now, attempt=False)   # 수동 실행은 자동 일정(재시도·mark_run)에 영향 없음
            finally:
                con.close()
        if res.result in ("ok", "partial"):
            out(f"백업 완료: {res.run_id} ({_size(res.bytes)})")
            if res.result == "partial":
                out(f"일부만 백업했습니다 — {bcopy.REASON_TEXT.get(res.reason, res.reason)}")
            return 0
        out(f"백업하지 못했습니다 — {bcopy.REASON_TEXT.get(res.reason, res.reason)}")
        return 1
    if cmd == "list":
        runs = store.list_runs(root)
        if not runs:
            out("백업이 없습니다")
            return 0
        mirror_ids = None
        if settings.backup_mirror_dir and store.mirror_ready(settings.backup_mirror_dir):
            mirror_ids = {r.run_id for r in store.list_runs(Path(settings.backup_mirror_dir))}
        for r in runs:
            size = sum(f.bytes for f in r.files.values())
            line = f"{r.run_id}  {_kst(r.created_at)}  {_size(size)}  ({', '.join(sorted(r.files))})"
            if settings.backup_mirror_dir:
                line += "  [미러: " + ("있음" if mirror_ids is not None and r.run_id in mirror_ids else "없음") + "]"
            out(line)
        return 0
    if cmd == "verify":
        key = _key(args, settings, out)
        if key is None:
            return 2
        rep = verify.verify_run(root, args.name, key, work_dir=Path(settings.backup_work_dir))
        if args.details:
            for kind, c in sorted(rep.files.items()):
                out(f"{kind}: {'정상' if c.ok else '문제 — ' + _VERIFY_TEXT.get(c.reason, c.reason)}")
                if kind == "core" and c.ok:
                    for t, n in sorted(c.tables.items()):
                        out(f"  {t}: {n}")
        if rep.ok:
            out("확인 완료")
            return 0
        for kind, c in sorted(rep.files.items()):
            if not c.ok:
                out(f"확인 실패: {_VERIFY_TEXT.get(c.reason, c.reason)}")
                break
        return 1
    if cmd == "keycheck":
        bk = settings.backup_key
        out("백업 키: " + (f"설정됨 (지문 {crypt.key_fingerprint(bk)})" if bk else "없음"))
        runs = store.list_runs(root)
        if not runs:
            out("최근 백업: 없음")
        elif bk:
            same = runs[-1].key_fp == crypt.key_fingerprint(bk)
            out("최근 백업의 키: " + ("일치" if same else "불일치 — 키를 바꾸셨다면 --ask-key 로 이전 키를 입력하세요"))
        return 0
    if cmd == "init-mirror":
        d = Path(args.dir)
        try:
            store.check_location(d, data_dir=settings.data_dir, repo_root=_repo_root())
            store.init_mirror(d)
        except store.UnsafeLocation as e:
            out(f"이 위치는 쓸 수 없습니다 — {e}")
            return 1
        out("외장 백업 위치를 준비했습니다.")
        out(f".env 에 PA_BACKUP_MIRROR_DIR={d} 를 넣고 서버를 다시 시작하세요")
        return 0
    if cmd == "restore":
        return _restore(args, settings, now, stdin, out, root)
    return 2


def _restore(args, settings, now, stdin, out, root: Path) -> int:
    if bool(args.to) == bool(args.replace_live):
        out("--to DIR (새 폴더로 복원) 또는 --replace-live (라이브 교체) 중 하나를 지정하세요")
        return 2
    key = _key(args, settings, out)
    if key is None:
        return 2
    only = (args.only,) if args.only else ("core",)
    work = Path(settings.backup_work_dir)
    mirror = Path(settings.backup_mirror_dir) if settings.backup_mirror_dir else None
    if args.to:
        try:
            res = restore.restore_to(root, args.name, key, Path(args.to), only=only, work_dir=work,
                                     data_dir=Path(settings.data_dir), mirror=mirror)
        except RestoreRefused as e:
            out(_refusal_text(e))
            return 1
        _audit(settings, now, "backup.restore", {"run_id": args.name, "mode": "new_dir", "only": list(only)})
        out(f"복원했습니다: {args.to} ({', '.join(res.files)})")
        out("복원본은 개인 기록 사본이므로 확인 후 직접 삭제하세요.")
        out("이 복원본을 실제로 쓰려면 서버를 끄고 `restore NAME --replace-live` 를 사용하세요.")
        return 0
    out("PA 서버를 먼저 끄세요(PA 창에서 Ctrl+C)")
    if restore.server_running():
        out(_REFUSAL["server_running"])
        return 2
    run_dir = store.resolve_run(root, args.name)
    info = store.parse_run(run_dir)
    when = _kst(info.created_at) if info else args.name
    out(f"백업 시각: {when} (KST)")
    out("경고: 이 백업 이후에 추가한 기록은 사라지고, 이후에 삭제한 개인 기록은 되살아납니다")
    out("계속하려면 '복원' 을 입력하세요: ")
    if stdin.readline().strip() != "복원":
        out("취소했습니다")
        return 1
    try:
        res = restore.replace_live(root, args.name, key, Path(settings.data_dir), only=only, now=now,
                                   work_dir=work, is_running=restore.server_running)
    except RestoreRefused as e:
        out(_refusal_text(e))
        return 2 if e.code == "server_running" else 1
    _audit(settings, now, "backup.restore", {"run_id": args.name, "mode": "replace_live", "only": list(only)})
    out(f"복원했습니다: {', '.join(res.files)}")
    out(f"이전 파일은 {res.pre_restore} 에 14일 보관한 뒤 자동 삭제됩니다. 서버를 다시 시작하세요.")
    return 0

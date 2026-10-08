"""백업 스케줄 잡: 매일 03:00 KST, 켜진 뒤 따라잡기, 재시도 상한, single-flight, 중립 알림.

pa.scheduler.due_jobs 는 근무일만 다루고 15분 유예 뒤 놓친 잡을 버리므로 백업은 자체 backup_due 를 쓰고 mark_run 만 공유한다.
알림은 앱 알림함에 고정 문구로만 남긴다.
"""
import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone

from pa.backup.runner import RunResult, note_attempt, record_result, run_backup, status
from pa.core.briefing import notify_gated
from pa.ids import new_id
from pa.scheduler import KST, mark_run

log = logging.getLogger(__name__)

BACKUP_JOB = "backup"
BACKUP_AT = time(3, 0)
RETRY_GAP = timedelta(hours=1)
MAX_ATTEMPTS = 3
MIRROR_ALERT_DAYS = 3
STALE_ALERT = timedelta(hours=48)
IMMEDIATE_REASONS = frozenset({"integrity", "verify", "too_large", "unsafe_location", "bad_key", "sqlite_old"})


def _pref(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM preference WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _dt(v: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def _attempts_today(con, today: str) -> int:
    d, _, n = (_pref(con, "backup.attempts") or "").partition(":")
    return int(n) if d == today and n.isdigit() else 0


def backup_due(con, now: datetime, settings) -> bool:
    if not getattr(settings, "backup_key", ""):
        return False
    n = now.astimezone(KST)
    if n < datetime.combine(n.date(), BACKUP_AT, KST):
        return False      # 00:00-02:59 에는 어제 몫을 하지 않는다(03:00 에 오늘 몫 실행)
    today = n.date().isoformat()
    if con.execute("SELECT 1 FROM job_run WHERE job_id=? AND for_date=?", (BACKUP_JOB, today)).fetchone():
        return False
    if _attempts_today(con, today) >= MAX_ATTEMPTS:
        return False
    last = _dt(_pref(con, "backup.last_attempt_at"))
    if last is not None and now - last < RETRY_GAP:
        return False
    return True


def _should_alert(con, res: RunResult, now: datetime) -> bool:
    today = now.astimezone(KST).date().isoformat()
    if res.result in ("failed", "partial") and (
            _attempts_today(con, today) >= MAX_ATTEMPTS or res.reason in IMMEDIATE_REASONS):
        return True
    st = status(con, enabled=True)
    if st.mirror_missing_since is not None and now - st.mirror_missing_since >= timedelta(days=MIRROR_ALERT_DAYS):
        return True
    if _pref(con, "backup.ever_ok") == "1" and st.last_ok_at is not None and now - st.last_ok_at >= STALE_ALERT:
        return True
    return False


async def run_backup_job(con, settings, rules, now: datetime, *, runner=None) -> RunResult | None:
    try:
        if not backup_due(con, now, settings):
            return None
        note_attempt(con, now)   # 실행 전에 기록: 도중에 프로세스가 죽어도 다음 시도는 1시간 뒤
        res = await asyncio.to_thread(runner or run_backup, settings, now)
        record_result(con, res, now, attempt=False)
        if res.result == "ok":
            mark_run(con, BACKUP_JOB, now)
        if _should_alert(con, res, now):
            day = now.astimezone(KST).date().isoformat()
            try:
                await notify_gated(con, "system", 1, f"backup_check:{day}", now, rules)
            except Exception:
                con.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES(?,?,?,?,?)",
                            (new_id(), now.astimezone(timezone.utc).isoformat(), "pa", "backup.notify_failed",
                             json.dumps({})))
        return res
    except Exception as e:   # 루프를 멈추지 않는다. 경로·내용이 섞이지 않게 예외 종류만 남긴다.
        log.error("backup job failed (%s)", type(e).__name__)
        return None


def spawn_backup_tick(state: dict, con, settings, rules, now: datetime) -> "asyncio.Task | None":
    """scout 와 같은 single-flight. 해당 없으면(키 없음·시간 전·이미 성공·재시도 간격) task 도 스레드도 만들지 않는다."""
    task = state.get("task")
    if task is not None and not task.done():
        return None
    if not backup_due(con, now, settings):
        return None
    state["task"] = asyncio.create_task(run_backup_job(con, settings, rules, now))
    return state["task"]

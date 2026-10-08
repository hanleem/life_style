"""생활 루틴 작업: 아침 물품 공급 점검 + 고정 문구 알림(kind, 개수, 경로만 담는다)."""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pa.logsafe import log_failure
from pa.clock import utcnow_iso
from pa.core import supply
from pa.core.briefing import notify_gated
from pa.core.notify import send_notify
from pa.core.quiet import should_deliver
from pa.ids import new_id
from pa.scheduler import mark_run

log = logging.getLogger("pa.routine")
KST = ZoneInfo("Asia/Seoul")
ROUTINE_JOBS = ("routine_morning",)
MORNING_OFFSET_MIN = 5


def _audit(con, event: str, kind: str) -> None:
    con.execute("INSERT INTO audit_log(id,at,actor,event,detail_json) VALUES(?,?,?,?,?)",
                (new_id(), utcnow_iso(), "pa", event, json.dumps({"kind": kind})))


def _morning_time(now: datetime, rules) -> datetime:
    n = now.astimezone(KST)
    base = datetime.combine(n.date(), rules.brief_morning, KST)
    return base - timedelta(minutes=MORNING_OFFSET_MIN)


def routine_due(con, now: datetime, rules) -> list[str]:
    n = now.astimezone(KST)
    done = {r[0] for r in con.execute("SELECT job_id FROM job_run WHERE for_date=?", (n.date().isoformat(),))}
    out = []
    if "routine_morning" not in done and n >= _morning_time(n, rules):
        out.append("routine_morning")
    return out


async def notify_routine(con, kind: str, count: int, dedupe_key: str, now: datetime, rules, *,
                         user_scheduled: bool = False) -> str:
    if user_scheduled and not kind.startswith("routine_"):
        raise ValueError("user_scheduled는 routine_* kind에만 허용됩니다")
    if kind == "routine_close" or user_scheduled:
        verdict = should_deliver(kind, now, rules, user_requested=True)
        if verdict != "deliver":
            _audit(con, "routine.notify_skipped", kind)
            return "skipped"
        await send_notify(con, kind, count, dedupe_key)
        return "deliver"
    return await notify_gated(con, kind, count, dedupe_key, now, rules)


async def run_routine_jobs(con, now: datetime, rules) -> list[str]:
    ran: list[str] = []
    for job in routine_due(con, now, rules):
        if not mark_run(con, job, now):
            continue
        ran.append(job)
        if job == "routine_morning":
            today = now.astimezone(KST).date()
            try:
                supply.consume_through(con, today, now=now)
            except Exception as e:
                log_failure(log, "routine: consume failed", e)
            try:
                if supply.is_meal_day(today):
                    items = supply.due_reminders(con, today)
                    if items:
                        supply.mark_reminded(con, [i.id for i in items], now=now)
                        await notify_routine(con, "routine_supply", len(items),
                                             f"routine_supply:{today.isoformat()}", now, rules)
            except Exception as e:
                log_failure(log, "routine: supply reminder failed", e)
    return ran

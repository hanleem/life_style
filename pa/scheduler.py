import sqlite3
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pa.core import meals
from pa.rules import Rules

KST = ZoneInfo("Asia/Seoul")
SCOUT_JOBS = ("scout_outing",)
JOBS = ("brief_morning", "brief_evening", "brief_friday", "deliver_held", "meal_plan", "meal_cart", "meal_grocery",
        "meal_delivery") + SCOUT_JOBS
THURSDAY = 3
GRACE_SECONDS = 900
FRIDAY = 4


def _sched_times(rules: Rules, weekday: int, scout=None) -> dict[str, time]:
    out: dict[str, time] = {}
    if weekday not in rules.workdays:
        return out
    if weekday in rules.brief_days:
        out["brief_morning"] = rules.brief_morning
        ev = rules.brief_evening_on(weekday)
        if ev is not None:
            out["brief_evening"] = ev
    if weekday == FRIDAY:
        out["brief_friday"] = rules.brief_friday
    # 보류 알림 묶음: 방해 금지 시간이 끝난 뒤 첫 시각
    out["deliver_held"] = rules.brief_morning
    for job, (day, at) in meals.schedule_times().items():   # 식단·주문 알림(config/meals.yaml)
        if day == weekday:
            out[job] = at
    if scout is not None:
        if weekday == THURSDAY and scout.outing.enabled:
            out["scout_outing"] = time(18, 0)
    return out


def _kst(now: datetime) -> datetime:
    return now.astimezone(KST) if now.tzinfo else now.replace(tzinfo=KST)


def due_jobs(con: sqlite3.Connection, now: datetime, rules: Rules, scout=None) -> list[str]:
    n = _kst(now)
    if n.weekday() not in rules.workdays:
        return []
    today = n.date()
    sched_times = _sched_times(rules, n.weekday(), scout)
    done = {r[0] for r in con.execute("SELECT job_id FROM job_run WHERE for_date=?", (today.isoformat(),))}
    out = []
    for job, t in sched_times.items():
        if job in done:
            continue
        sched = datetime.combine(today, t, KST)
        if sched <= n < sched + timedelta(seconds=GRACE_SECONDS):
            out.append(job)
    return out


def mark_run(con: sqlite3.Connection, job_id: str, now: datetime) -> bool:
    n = _kst(now)
    cur = con.execute(
        "INSERT OR IGNORE INTO job_run(job_id, for_date, ran_at) VALUES(?,?,?)",
        (job_id, n.date().isoformat(), n.astimezone(timezone.utc).isoformat()),
    )
    return cur.rowcount == 1

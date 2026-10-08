from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
FOLD_KINDS = ("task_update", "info")


def should_deliver(kind: str, now: datetime, rules, *, urgent: bool = False,
                   user_requested: bool = False,
                   quiet_mode: bool = False) -> Literal["deliver", "hold", "fold"]:
    t = now.astimezone(KST)
    if quiet_mode:
        return "deliver" if urgent else "hold"
    end = rules.work_end_on(t.weekday())
    if (t.weekday() in rules.rest_days or end is None) and not urgent:
        return "hold"
    off_hours = end is None or t.time() >= end or t.time() < rules.protected_until
    if off_hours and not (urgent or user_requested):
        return "hold"
    if kind in FOLD_KINDS:
        return "fold"
    return "deliver"

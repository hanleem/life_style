import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

log = logging.getLogger("pa.rules")
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_LEGACY_KEYS = ("work_hours", "quiet_after")


@dataclass(frozen=True)
class Rules:
    work_start: time
    protected_until: time
    work_end_by_day: dict[int, time]
    workdays: frozenset[int]
    rest_days: frozenset[int]
    brief_days: frozenset[int]
    brief_morning: time
    brief_evening_before_end_min: int
    brief_friday: time
    max_today_must: int

    def work_end_on(self, weekday: int) -> time | None:
        return self.work_end_by_day.get(weekday)

    def brief_evening_on(self, weekday: int) -> time | None:
        end = self.work_end_on(weekday)
        if end is None or weekday not in self.brief_days:
            return None
        base = datetime.combine(date(2000, 1, 1), end) - timedelta(minutes=self.brief_evening_before_end_min)
        return base.time()


def _t(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def _day(d) -> int:
    return _DAYS[str(d).strip().lower()[:3]]


def _days(items) -> frozenset[int]:
    return frozenset(_day(d) for d in items or [])


def load_rules(path: Path | None = None) -> Rules:
    data = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    for k in _LEGACY_KEYS:
        if k in data:
            log.warning("settings: 구 키 %r 는 무시됩니다 (요일별 work_end 사용)", k)
    br = data["briefing"]
    ends = {_day(d): _t(v) for d, v in data["work_end"].items()}
    return Rules(
        work_start=_t(data["work_start"]),
        protected_until=_t(data["protected_until"]),
        work_end_by_day=ends,
        workdays=frozenset(ends),
        rest_days=_days(data.get("rest_days")),
        brief_days=_days(br["days"]),
        brief_morning=_t(br["morning"]),
        brief_evening_before_end_min=int(br["evening_before_end_min"]),
        brief_friday=_t(br["friday"]),
        max_today_must=int(data["max_today_must"]),
    )

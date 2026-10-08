from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class CalEvent:
    id: str
    calendar_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    description: str = ""      # 휴일 캘린더 구분용(첫 줄이 '공휴일' 또는 '기념일'); 일정 본문은 쓰지 않는다


class CalendarClient(Protocol):
    async def events_between(self, start: datetime, end: datetime) -> list[CalEvent]: ...
    async def insert_idempotent(
        self, action_id: str, calendar_id: str, title: str, start: datetime, end: datetime
    ) -> str: ...
    async def get_by_action(self, action_id: str) -> str | None: ...


class FakeCalendar:
    def __init__(self) -> None:
        self.events: list[CalEvent] = []
        self._by_action: dict[str, str] = {}
        self._n = 0

    async def events_between(self, start: datetime, end: datetime) -> list[CalEvent]:
        return sorted((e for e in self.events if e.start < end and e.end > start), key=lambda e: e.start)

    async def insert_idempotent(
        self, action_id: str, calendar_id: str, title: str, start: datetime, end: datetime
    ) -> str:
        if action_id in self._by_action:
            return self._by_action[action_id]
        self._n += 1
        eid = f"fake-{self._n}"
        self.events.append(CalEvent(eid, calendar_id, title, start, end, False))
        self._by_action[action_id] = eid
        return eid

    async def get_by_action(self, action_id: str) -> str | None:
        return self._by_action.get(action_id)

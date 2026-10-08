from datetime import datetime as _datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CalendarEventPayload(BaseModel):
    kind: Literal["calendar_event"] = "calendar_event"
    calendar_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    start: str
    end: str

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("제목이 비어 있습니다")
        return v

    @field_validator("start", "end")
    @classmethod
    def _aware_iso(cls, v: str) -> str:
        d = _datetime.fromisoformat(v)
        if d.tzinfo is None:
            raise ValueError("시각에는 시간대가 필요합니다")
        return v

    @model_validator(mode="after")
    def _order(self):
        if _datetime.fromisoformat(self.end) <= _datetime.fromisoformat(self.start):
            raise ValueError("종료 시각은 시작 시각보다 늦어야 합니다")
        return self


DraftPayload = CalendarEventPayload

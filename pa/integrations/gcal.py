"""Google Calendar REST client (httpx) implementing CalendarClient."""
import hashlib
import logging
from datetime import date, datetime
from typing import Awaitable, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from pa.integrations.calendar import CalEvent

KST = ZoneInfo("Asia/Seoul")
API = "https://www.googleapis.com/calendar/v3/calendars"
MAX_PAGES = 10
log = logging.getLogger(__name__)


def _event_id(action_id: str) -> str:
    return "pa" + hashlib.sha1(action_id.encode()).hexdigest()[:24]


def _parse(v: dict) -> tuple[datetime, bool]:
    if v.get("dateTime"):
        return datetime.fromisoformat(v["dateTime"].replace("Z", "+00:00")), False
    d = date.fromisoformat(v["date"])
    return datetime(d.year, d.month, d.day, tzinfo=KST), True


class GoogleCalendar:
    def __init__(self, token_fn: Callable[[], Awaitable[str | None]], read_ids: list[str] | None = None,
                 write_ids: "list[str] | Callable[[], list[str]] | None" = None):
        self._token_fn = token_fn
        self.read_ids = list(read_ids or ["primary"])
        self._write_ids = write_ids
        self._warned = False

    def _writes(self) -> list[str]:
        w = self._write_ids() if callable(self._write_ids) else self._write_ids
        return list(w) if w else ["primary"]

    async def _headers(self) -> dict:
        tok = await self._token_fn()
        if not tok:
            raise ConnectionError("Google 미연결")
        return {"Authorization": f"Bearer {tok}"}

    @staticmethod
    def _url(cal_id: str, suffix: str = "") -> str:
        return f"{API}/{quote(cal_id, safe='')}/events{suffix}"

    async def events_between(self, start: datetime, end: datetime) -> list[CalEvent]:
        headers = await self._headers()
        out: list[CalEvent] = []
        async with httpx.AsyncClient(timeout=20) as http:
            for cid in self.read_ids:
                token = None
                for _ in range(MAX_PAGES):
                    params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                              "singleEvents": "true", "orderBy": "startTime"}
                    if token:
                        params["pageToken"] = token
                    r = await http.get(self._url(cid), params=params, headers=headers)
                    r.raise_for_status()
                    data = r.json()
                    for it in data.get("items", []):
                        try:
                            s, ad = _parse(it["start"])
                            e, _ = _parse(it["end"])
                            out.append(CalEvent(it["id"], cid, it.get("summary", ""), s, e, ad,
                                            (it.get("description") or "").split("\n", 1)[0][:20]))
                        except (KeyError, ValueError, TypeError, AttributeError):
                            if not self._warned:
                                self._warned = True
                                log.warning("skipping malformed calendar event")
                    token = data.get("nextPageToken")
                    if not token:
                        break
        out.sort(key=lambda e: e.start)
        return out

    async def insert_idempotent(self, action_id: str, calendar_id: str, title: str,
                                start: datetime, end: datetime) -> str:
        headers = await self._headers()
        eid = _event_id(action_id)
        body = {"id": eid, "summary": title,
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
                "extendedProperties": {"private": {"pa_action_id": action_id}}}
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(self._url(calendar_id), json=body, headers=headers)
        if r.status_code == 409:
            async with httpx.AsyncClient(timeout=20) as http:
                g = await http.get(self._url(calendar_id, f"/{eid}"), headers=headers)
            g.raise_for_status()
            ev = g.json()
            priv = (ev.get("extendedProperties") or {}).get("private") or {}
            if priv.get("pa_action_id") != action_id or ev.get("status") == "cancelled":
                raise RuntimeError("calendar insert conflict: existing event does not match action")
            return eid
        r.raise_for_status()
        return r.json().get("id", eid)

    async def get_by_action(self, action_id: str) -> str | None:
        headers = await self._headers()
        eid = _event_id(action_id)
        async with httpx.AsyncClient(timeout=20) as http:
            for cid in self._writes():
                r = await http.get(self._url(cid, f"/{eid}"), headers=headers)
                if r.status_code in (404, 410):
                    continue
                r.raise_for_status()
                return r.json().get("id", eid)
        return None

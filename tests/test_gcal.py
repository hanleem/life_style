import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from pa.integrations.gcal import GoogleCalendar

BASE = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
T0 = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)


async def _tok():
    return "T"


async def _none():
    return None


@respx.mock
async def test_list_parses_all_day_and_timed():
    respx.get(BASE).mock(return_value=httpx.Response(200, json={"items": [
        {"id": "a", "summary": "연구회의", "start": {"dateTime": "2026-10-07T08:00:00Z"}, "end": {"dateTime": "2026-10-07T09:00:00Z"}},
        {"id": "b", "summary": "한글날", "start": {"date": "2026-10-09"}, "end": {"date": "2026-10-10"}}]}))
    ev = await GoogleCalendar(_tok).events_between(T0, T0 + timedelta(days=7))
    assert [(e.id, e.all_day) for e in ev] == [("a", False), ("b", True)]
    assert ev[0].start == datetime(2026, 10, 7, 8, tzinfo=timezone.utc) and ev[0].start.utcoffset() is not None
    assert ev[1].start.utcoffset() == timedelta(hours=9) and ev[1].start.hour == 0
    assert ev[1].end.day == 10


@respx.mock
async def test_list_paginates_and_sends_params():
    route = respx.get(BASE).mock(side_effect=[
        httpx.Response(200, json={"items": [{"id": "a", "summary": "x", "start": {"date": "2026-10-08"}, "end": {"date": "2026-10-09"}}],
                                  "nextPageToken": "N"}),
        httpx.Response(200, json={"items": [{"id": "b", "start": {"date": "2026-10-09"}, "end": {"date": "2026-10-10"}}]})])
    ev = await GoogleCalendar(_tok).events_between(T0, T0 + timedelta(days=7))
    assert [e.id for e in ev] == ["a", "b"] and ev[1].title == ""
    q = route.calls[0].request.url.params
    assert q["singleEvents"] == "true" and q["orderBy"] == "startTime"
    assert route.calls[1].request.url.params["pageToken"] == "N"
    assert route.calls[0].request.headers["Authorization"] == "Bearer T"


@respx.mock
async def test_insert_conflict_returns_same_id_and_deterministic():
    route = respx.post(BASE).mock(return_value=httpx.Response(409, json={}))
    eid = "pa" + __import__("hashlib").sha1(b"act1").hexdigest()[:24]
    respx.get(f"{BASE}/{eid}").mock(return_value=httpx.Response(200, json={
        "id": eid, "extendedProperties": {"private": {"pa_action_id": "act1"}}}))
    i1 = await GoogleCalendar(_tok).insert_idempotent("act1", "primary", "면담", T0, T0 + timedelta(hours=1))
    body = json.loads(route.calls[0].request.content)
    assert i1 == body["id"] and body["id"].startswith("pa") and body["extendedProperties"]["private"]["pa_action_id"] == "act1"
    assert route.calls[0].request.headers["Authorization"] == "Bearer T"


@respx.mock
async def test_get_by_action():
    cal = GoogleCalendar(_tok)
    eid = "pa" + __import__("hashlib").sha1(b"act1").hexdigest()[:24]
    respx.get(f"{BASE}/{eid}").mock(return_value=httpx.Response(200, json={"id": eid}))
    assert await cal.get_by_action("act1") == eid
    respx.get(f"{BASE}/{eid}").mock(return_value=httpx.Response(404))
    assert await cal.get_by_action("act1") is None


@respx.mock
async def test_insert_server_error_raises():
    respx.post(BASE).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await GoogleCalendar(_tok).insert_idempotent("a", "primary", "t", T0, T0 + timedelta(hours=1))


async def test_no_token_raises():
    with pytest.raises(ConnectionError):
        await GoogleCalendar(_none).events_between(T0, T0)


@respx.mock
async def test_get_by_action_probes_all_write_ids():
    eid = "pa" + __import__("hashlib").sha1(b"a1").hexdigest()[:24]
    B2 = "https://www.googleapis.com/calendar/v3/calendars/other%40example.com/events"
    respx.get(f"{BASE}/{eid}").mock(return_value=httpx.Response(404))
    respx.get(f"{B2}/{eid}").mock(return_value=httpx.Response(200, json={"id": eid}))
    cal = GoogleCalendar(_tok, write_ids=lambda: ["primary", "other@example.com"])
    assert await cal.get_by_action("a1") == eid


def _conflict(ev):
    eid = "pa" + __import__("hashlib").sha1(b"act1").hexdigest()[:24]
    respx.post(BASE).mock(return_value=httpx.Response(409, json={}))
    respx.get(f"{BASE}/{eid}").mock(return_value=httpx.Response(200, json=ev))


@respx.mock
async def test_conflict_matching_event_ok():
    _conflict({"id": "x", "status": "confirmed", "extendedProperties": {"private": {"pa_action_id": "act1"}}})
    assert (await GoogleCalendar(_tok).insert_idempotent("act1", "primary", "t", T0, T0 + timedelta(hours=1))).startswith("pa")


@respx.mock
@pytest.mark.parametrize("ev", [
    {"status": "confirmed", "extendedProperties": {"private": {"pa_action_id": "other"}}},
    {"status": "cancelled", "extendedProperties": {"private": {"pa_action_id": "act1"}}},
    {"status": "confirmed"},
])
async def test_conflict_mismatch_or_cancelled_raises(ev):
    _conflict(ev)
    with pytest.raises(RuntimeError):
        await GoogleCalendar(_tok).insert_idempotent("act1", "primary", "t", T0, T0 + timedelta(hours=1))


@respx.mock
async def test_malformed_event_skipped():
    respx.get(BASE).mock(return_value=httpx.Response(200, json={"items": [
        {"id": "bad", "start": {"date": "nope"}, "end": {"date": "2026-10-10"}},
        {"id": "noend", "start": {"date": "2026-10-09"}},
        {"id": "ok", "start": {"date": "2026-10-09"}, "end": {"date": "2026-10-10"}}]}))
    ev = await GoogleCalendar(_tok).events_between(T0, T0 + timedelta(days=7))
    assert [e.id for e in ev] == ["ok"]

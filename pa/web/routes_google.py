"""Google 캘린더 연결(선택 기능). 연결·콜백은 이 PC(루프백)에서만 완료할 수 있다."""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pa.clock import utcnow_iso
from pa.core.calendar_actions import write_allowlist
from pa.core.errors import Forbidden
from pa.integrations.google_auth import SCOPES, GoogleAuth
from pa.web.auth import is_loopback as _is_loopback
from pa.web.auth import require_owner
from pa.web.ui import make_templates

log = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()
_LABELS = {
    "https://www.googleapis.com/auth/calendar.readonly": "캘린더 읽기",
    "https://www.googleapis.com/auth/calendar.events": "캘린더 일정 쓰기",
}
CAL_WRITE = SCOPES["calwrite"][0]


def _auth(request: Request) -> GoogleAuth:
    return GoogleAuth(request.app.state.settings, request.app.state.con)


@router.get("/settings/google", response_class=HTMLResponse)
async def settings_google(request: Request, _: str = Depends(require_owner)):
    g = _auth(request)
    granted = g.granted()
    return templates.TemplateResponse(request, "settings_google.html", {
        "problem": g.config_problem(), "connected": g.connected(),
        "labels": [_LABELS.get(s, s) for s in granted],
        "cal_write": CAL_WRITE in granted, "write_ids": write_allowlist(request.app.state.con)})


@router.get("/auth/google/start")
async def google_start(request: Request, level: str = "base", _: str = Depends(require_owner)):
    try:
        url = _auth(request).start([level])
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Forbidden as e:
        raise HTTPException(403, str(e))
    return RedirectResponse(url, status_code=303)


@router.get("/auth/google/callback", response_class=HTMLResponse)
async def google_callback(request: Request, state: str = "", code: str = ""):
    if not _is_loopback(request):
        raise HTTPException(403, "이 PC에서만 연결을 완료할 수 있습니다")
    if not state or not code:
        raise HTTPException(400, "잘못된 요청입니다")
    try:
        await asyncio.to_thread(_auth(request).finish, state, code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Forbidden as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        log.warning("google callback failed (%s)", type(e).__name__)
        raise HTTPException(400, "Google 연결에 실패했습니다. 다시 시도해 주세요")
    return templates.TemplateResponse(request, "google_done.html", {})


@router.post("/settings/google/calendar-write")
async def calendar_write(request: Request, ids: list[str] = Form(default=[]), _: str = Depends(require_owner)):
    if CAL_WRITE not in _auth(request).granted():
        raise HTTPException(400, "캘린더 쓰기 권한이 아직 연결되지 않았습니다")
    clean = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
    request.app.state.con.execute(
        "INSERT INTO preference(key,value,updated_at) VALUES('calendar.write_ids',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (json.dumps(clean), utcnow_iso()))
    return RedirectResponse("/settings/google", status_code=303)

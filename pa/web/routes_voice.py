"""Briefing voice: script JSON, settings page and settings API. Every route is owner-only and `no-store`.
This module never creates audio (V3 owns /api/voice/briefing/audio) and never calls an LLM or writes briefing data."""
import functools
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from pa.clock import utcnow
from pa.rules import load_rules
from pa.voice import prefs, source, voice_copy
from pa.web.auth import require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_SWITCHES = ("say_todo_titles", "say_event_titles", "say_supply")


def _now() -> datetime:
    return utcnow()


@functools.lru_cache(maxsize=1)
def _default_rules():
    return load_rules()


def _rules(request: Request):
    return getattr(request.app.state, "rules", None) or _default_rules()


def _json(data, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=_NO_STORE)


@router.get("/api/voice/briefing")
async def briefing_script(request: Request, kind: str | None = None, _: str = Depends(require_owner)):
    con = request.app.state.con
    settings = prefs.get_settings(con)
    if not settings.enabled:
        return _json({"available": False, "reason": "disabled"})
    if kind is not None and kind not in source.KINDS:
        raise HTTPException(422, "브리핑 종류가 올바르지 않습니다", headers=_NO_STORE)
    sc = source.load_today(con, _now(), _rules(request), settings, kind=kind)
    if sc is None:
        return _json({"available": False, "reason": "no_briefing"})
    return _json({"available": True, "reason": None, "briefing_kind": sc.kind, "for_date": sc.for_date.isoformat(),
                  "sentences": list(sc.sentences), "est_seconds": sc.est_seconds, "script_hash": sc.script_hash,
                  "rate": settings.rate, "engine_ready": bool(request.app.state.tts.available())})


@router.get("/settings/voice")
async def settings_page(request: Request, _: str = Depends(require_owner)):
    s = prefs.get_settings(request.app.state.con)
    resp = templates.TemplateResponse(request, "settings_voice.html", {
        "s": s, "copy": voice_copy.SETTINGS_COPY, "engines": prefs.ENGINES, "rates": prefs.RATES, "switches": _SWITCHES,
        "warning": voice_copy.SENSITIVE_WARNING, "local_only": voice_copy.LOCAL_ONLY,
        "engine_name": getattr(request.app.state.tts, "name", "none"),
        "warn_on": ("say_todo_titles", "say_event_titles")})
    resp.headers.update(_NO_STORE)
    return resp


@router.put("/api/voice/settings")
async def save_settings(request: Request, _: str = Depends(require_owner)):
    if not (request.headers.get("content-type") or "").lower().startswith("application/json"):
        raise HTTPException(415, "JSON 형식으로 보내 주세요", headers=_NO_STORE)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON 형식이 올바르지 않습니다", headers=_NO_STORE) from None
    if not isinstance(body, dict):
        raise HTTPException(400, "설정은 이름과 값의 목록이어야 합니다", headers=_NO_STORE)
    con = request.app.state.con
    try:
        s = prefs.set_settings(con, **body) if body else prefs.get_settings(con)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e), headers=_NO_STORE) from None
    return _json({"enabled": s.enabled, "engine": s.engine, "rate": s.rate, **{k: getattr(s, k) for k in _SWITCHES}})

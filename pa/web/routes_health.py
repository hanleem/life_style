"""점검 화면(/settings/health): 백업 상태 한 줄 + 보존 안내. 소유자 전용, 읽기 전용.
경로·실행 이름·파일명·키 지문·표 이름·행 수는 보여 주지 않고 복원 버튼도 없다(복원은 CLI 만)."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pa.backup import copy as bcopy
from pa.backup.runner import status
from pa.backup.store import KST
from pa.clock import utcnow
from pa.web.auth import require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()
STALE = timedelta(hours=48)


def _now():
    return utcnow()


def _size(n: int | None) -> str:
    return "-" if n is None else f"{n / (1024 * 1024):.1f} MB"


def _when(dt) -> str:
    k = dt.astimezone(KST)
    return f"{k.month}월 {k.day}일 {k:%H:%M}"


def backup_view(con, settings, now) -> dict:
    enabled = bool(settings.backup_key)
    st = status(con, enabled=enabled)
    view = {"line": "", "check": None, "reason": None, "mirror": None}
    if not enabled:
        view["line"] = bcopy.BACKUP_OFF
        return view
    view["line"] = (bcopy.BACKUP_NEVER if st.last_ok_at is None
                    else bcopy.BACKUP_OK.format(when=_when(st.last_ok_at), size=_size(st.last_bytes)))
    stale = st.last_ok_at is not None and now - st.last_ok_at > STALE
    if st.last_result in ("failed", "partial") or stale:
        view["check"] = bcopy.BACKUP_CHECK
        view["reason"] = bcopy.REASON_TEXT.get(st.last_reason or "")
    if st.mirror_state == "missing":
        view["mirror"] = bcopy.MIRROR_MISSING
    elif st.mirror_state == "ok":
        view["mirror"] = bcopy.MIRROR_OK
    return view


@router.get("/settings/health", response_class=HTMLResponse)
async def settings_health(request: Request, _: str = Depends(require_owner)):
    s, con = request.app.state.settings, request.app.state.con
    return templates.TemplateResponse(request, "settings_health.html", {
        "backup": backup_view(con, s, _now()), "retention_note": bcopy.RETENTION_NOTE})

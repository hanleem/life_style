"""가정통신문 화면: 사진 올리기 → 후보 확인 → 할 일 후보 / 일정 초안(승인 필요). 소유자 전용.

사진은 메모리에서 바로 AI 에 넘기고 처리 직후 지운다(pa.core.notice.extract). 화면의 결과 문구는 코드로 고른 고정 문장이다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from pa.core import kids, notice, profile
from pa.core.errors import Forbidden
from pa.web.auth import require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()
KST = ZoneInfo("Asia/Seoul")
MESSAGES = {"empty": "사진에서 일정이나 준비물을 찾지 못했어요. 글자가 잘 보이게 다시 찍어 주세요.",
            "fail": "사진을 읽지 못했어요. 잠시 뒤 다시 올려 주세요.",
            "big": "사진이 너무 커요. 8MB 이하로 올려 주세요.",
            "type": "사진(JPEG·PNG·WEBP) 파일을 골라 주세요."}


def _prof(request: Request) -> dict:
    return profile.load(request.app.state.settings.data_dir) or profile.defaults()


def _now() -> datetime:
    return datetime.now(KST)


@router.get("/notice")
async def notice_page(request: Request, r: str = "", n: int = 0, _: str = Depends(require_owner)):
    con = request.app.state.con
    message = f"가정통신문에서 {min(max(n, 0), 99)}건의 후보를 찾았어요. 확인해 주세요." if r == "ok" else MESSAGES.get(r)
    events = notice.list_pending(con)
    resp = templates.TemplateResponse(request, "notice.html", {
        "events": events, "message": message, "children": kids.child_labels(_prof(request)),
        "calendar_ready": notice.calendar_ready(con)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def _read_limited(upload) -> bytes | None:
    """업로드를 한도까지만 읽는다(너무 크면 None). 큰 파일을 통째로 메모리에 올리지 않는다."""
    buf = bytearray()
    while chunk := await upload.read(1024 * 1024):
        buf += chunk
        if len(buf) > notice.MAX_IMAGE_BYTES:
            return None
    return bytes(buf)


@router.post("/notice")
async def notice_upload(request: Request, _: str = Depends(require_owner)):
    form = await request.form()
    photo = form.get("photo")
    if photo is None or not hasattr(photo, "read"):
        return RedirectResponse("/notice?r=type", status_code=303)
    data = await _read_limited(photo)
    if data is None:
        return RedirectResponse("/notice?r=big", status_code=303)
    if notice.sniff_image(data[:16]) is None:
        return RedirectResponse("/notice?r=type", status_code=303)
    router_ = request.app.state.router
    try:
        ids = await notice.extract(request.app.state.con, router_, data, _prof(request), _now()) if router_ is not None else None
    except ValueError:
        return RedirectResponse("/notice?r=type", status_code=303)
    except Exception:  # noqa: BLE001 - AI 호출 실패는 화면 문구로만 알린다(내용은 기록하지 않는다)
        ids = None
    if ids is None:
        return RedirectResponse("/notice?r=fail", status_code=303)
    return RedirectResponse(f"/notice?r=ok&n={len(ids)}" if ids else "/notice?r=empty", status_code=303)


def _event(request: Request, event_id: str) -> dict:
    try:
        return notice.get(request.app.state.con, event_id)
    except KeyError:
        raise HTTPException(404, "찾을 수 없는 항목이에요") from None


@router.post("/api/notice/{event_id}/tasks")
async def notice_tasks(event_id: str, request: Request, _: str = Depends(require_owner)):
    _event(request, event_id)
    ids = notice.add_tasks(request.app.state.con, event_id, _now(), _prof(request))
    return {"ok": True, "count": len(ids)}


@router.post("/api/notice/{event_id}/calendar")
async def notice_calendar(event_id: str, request: Request, _: str = Depends(require_owner)):
    _event(request, event_id)
    try:
        did = notice.add_calendar(request.app.state.con, event_id, _prof(request))
    except Forbidden as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True, "draft_id": did}


@router.post("/api/notice/{event_id}/child")
async def notice_child(event_id: str, request: Request, _: str = Depends(require_owner)):
    _event(request, event_id)
    try:
        body = await request.json()
        notice.set_child(request.app.state.con, event_id, body.get("index") if isinstance(body, dict) else None)
    except ValueError:
        raise HTTPException(400, "아이를 다시 골라 주세요") from None
    return JSONResponse({"ok": True})


@router.post("/api/notice/{event_id}/finish")
async def notice_finish(event_id: str, request: Request, _: str = Depends(require_owner)):
    _event(request, event_id)
    notice.finish(request.app.state.con, event_id)
    return {"ok": True}


@router.post("/api/notice/{event_id}/dismiss")
async def notice_dismiss(event_id: str, request: Request, _: str = Depends(require_owner)):
    _event(request, event_id)
    notice.dismiss(request.app.state.con, event_id)
    return {"ok": True}

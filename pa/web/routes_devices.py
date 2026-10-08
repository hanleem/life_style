"""기기 등록(QR): PC 에서 QR 을 만들고, 폰이 찍어 등록한다. /devices 는 소유자 전용, /join 은 일회용 토큰이 곧 자격이다."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from pa.core import devices
from pa.web.auth import require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()


def _page(request: Request, name: str, ctx: dict, status: int = 200):
    resp = templates.TemplateResponse(request, name, ctx, status_code=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _devices_ctx(request: Request, **extra) -> dict:
    return {"devices": devices.list_devices(request.app.state.con), "welcome_mode": False, "qr": None, "no_address": False, **extra}


@router.get("/devices")
async def devices_page(request: Request, _: str = Depends(require_owner)):
    return _page(request, "devices.html", _devices_ctx(request))


@router.post("/devices/new")
async def devices_new(request: Request, _: str = Depends(require_owner)):
    """QR 을 만든다. 토큰은 주소 안(QR)에만 있고 화면 글자로는 보이지 않는다."""
    con = request.app.state.con
    url = devices.join_url(request.app.state.settings, devices.create_join_token(con))
    if url is None:
        return _page(request, "devices.html", _devices_ctx(request, no_address=True))
    return _page(request, "devices.html", _devices_ctx(request, qr=devices.qr_svg(url)))


@router.post("/devices/{device_id}/revoke")
async def devices_revoke(device_id: str, request: Request, _: str = Depends(require_owner)):
    if not devices.revoke(request.app.state.con, device_id):
        raise HTTPException(404, "찾을 수 없는 기기예요")
    return RedirectResponse("/devices", status_code=303)


@router.get("/join/{token}")
async def join_page(token: str, request: Request):
    if not devices.join_token_valid(request.app.state.con, token):
        return _page(request, "join_invalid.html", {"welcome_mode": True}, status=410)
    return _page(request, "join.html", {"token": token, "welcome_mode": True, "default_name": "내 아이폰"})


@router.post("/join/{token}")
async def join_save(token: str, request: Request):
    con = request.app.state.con
    form = await request.form()
    if not devices.consume_join_token(con, token):
        return _page(request, "join_invalid.html", {"welcome_mode": True}, status=410)
    did = devices.add_device(con, str(form.get("name", "")))
    request.session["owner"] = True
    request.session["device"] = did
    return RedirectResponse("/install", status_code=303)

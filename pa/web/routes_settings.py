"""설정 입구: 흩어져 있던 설정 화면으로 가는 링크. 소유자 전용."""
from fastapi import APIRouter, Depends, Request

from pa.web.auth import require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()


@router.get("/settings")
async def settings_hub(request: Request, _: str = Depends(require_owner)):
    resp = templates.TemplateResponse(request, "settings.html", {})
    resp.headers["Cache-Control"] = "no-store"
    return resp

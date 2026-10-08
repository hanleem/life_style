"""설정 입구: 흩어져 있던 설정 화면으로 가는 링크 + 보이는 기능(관심사) 켜고 끄기. 소유자 전용."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from pa.core import profile
from pa.web.auth import require_owner
from pa.web.routes_welcome import apply_profile
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()


@router.get("/settings")
async def settings_hub(request: Request, _: str = Depends(require_owner)):
    resp = templates.TemplateResponse(request, "settings.html", {})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/settings/features")
async def features_page(request: Request, _: str = Depends(require_owner)):
    from pa.core import home_cards
    prof = profile.load(request.app.state.settings.data_dir) or profile.defaults()
    return templates.TemplateResponse(request, "settings_features.html", {
        "chosen": prof["interests"], "interests": profile.INTERESTS, "ready": home_cards.IMPLEMENTED})


@router.post("/settings/features")
async def features_save(request: Request, _: str = Depends(require_owner)):
    data_dir = request.app.state.settings.data_dir
    prof = profile.load(data_dir) or profile.defaults()
    form = await request.form()
    try:
        new = profile.save(data_dir, {**prof, "interests": form.getlist("interests")})
    except profile.ProfileError as e:
        raise HTTPException(400, str(e)) from None
    apply_profile(request, new)
    return RedirectResponse("/settings/features", status_code=303)

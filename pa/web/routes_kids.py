"""아이 루틴 화면: 오늘 시간표·내일 준비물(/kids)과 설정(/kids/settings). 소유자 전용."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from pa.core import kids, notice, profile
from pa.web.auth import require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()
KST = ZoneInfo("Asia/Seoul")
_BLANK_CLASS = {"name": "", "days": [], "start": "", "bring": []}


def _today() -> date:
    return datetime.now(KST).date()


def _prof(request: Request) -> dict:
    return profile.load(request.app.state.settings.data_dir) or profile.defaults()


def _grouped(items: list[dict]) -> list[tuple[str, list[dict]]]:
    out: dict[str, list[dict]] = {}
    for i in items:
        out.setdefault(i["child"], []).append(i)
    return list(out.items())


def _page(request: Request, name: str, ctx: dict, status: int = 200):
    resp = templates.TemplateResponse(request, name, ctx, status_code=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/kids")
async def kids_page(request: Request, _: str = Depends(require_owner)):
    con, prof = request.app.state.con, _prof(request)
    today = _today()
    tomorrow = today + timedelta(days=1)
    day_name = lambda d: kids.DAY_LABEL[kids.DAYS[d.weekday()]]
    return _page(request, "kids.html", {
        "today_label": day_name(today), "tomorrow_label": day_name(tomorrow),
        "today": kids.day_plan(con, today, prof), "tomorrow": kids.day_plan(con, tomorrow, prof),
        "tomorrow_groups": _grouped(kids.day_plan(con, tomorrow, prof)["items"]), "pending_notices": notice.pending_count(con),
        "has_children": bool(kids.child_labels(prof))})


def _view(data: dict, n: int) -> list[dict]:
    """프로필의 아이 수만큼, 저장된 값(없으면 평일 등교 기본)으로 채운 폼용 자료. 학원 줄은 빈 줄까지 채운다."""
    out = []
    for i in range(n):
        c = data["children"][i] if i < len(data["children"]) else {"school_days": list(kids.WEEKDAYS), "school_start": "", "school_end": "", "classes": []}
        classes = list(c.get("classes") or []) + [_BLANK_CLASS] * (kids.MAX_CLASSES - len(c.get("classes") or []))
        out.append({**c, "classes": classes[:kids.MAX_CLASSES],
                    "filled": max(1, sum(1 for k in c.get("classes") or [] if k.get("name")) + 1)})
    return out


def _settings_ctx(request: Request, data: dict, error: str | None = None) -> dict:
    labels = kids.child_labels(_prof(request))
    return {"labels": labels, "children": _view(data, len(labels)), "prep_time": data.get("prep_time") or kids.DEFAULT_PREP,
            "error": error, "days": kids.DAYS, "day_label": kids.DAY_LABEL, "max_classes": kids.MAX_CLASSES}


@router.get("/kids/settings")
async def kids_settings(request: Request, _: str = Depends(require_owner)):
    return _page(request, "kids_settings.html", _settings_ctx(request, kids.load(request.app.state.con)))


def _form_to_raw(form, n: int) -> dict:
    children = []
    for i in range(n):
        classes = [{"name": form.get(f"c{i}_k{j}_name", ""), "days": form.getlist(f"c{i}_k{j}_days"),
                    "start": form.get(f"c{i}_k{j}_start", ""), "bring": form.get(f"c{i}_k{j}_bring", "")}
                   for j in range(kids.MAX_CLASSES)]
        children.append({"school_days": form.getlist(f"c{i}_days"), "school_start": form.get(f"c{i}_start", ""),
                         "school_end": form.get(f"c{i}_end", ""), "classes": classes})
    return {"prep_time": form.get("prep_time", ""), "children": children}


@router.post("/kids/settings")
async def kids_settings_save(request: Request, _: str = Depends(require_owner)):
    n = len(kids.child_labels(_prof(request)))
    raw = _form_to_raw(await request.form(), n)
    try:
        kids.save(request.app.state.con, raw)
    except kids.KidsError as e:
        shown = {"prep_time": raw["prep_time"], "children": [
            {**c, "classes": [{**k, "bring": [b for b in str(k["bring"]).split(",") if b.strip()]} for k in c["classes"]]}
            for c in raw["children"]]}
        return _page(request, "kids_settings.html", _settings_ctx(request, shown, str(e)), status=400)
    return RedirectResponse("/kids", status_code=303)

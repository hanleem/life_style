"""첫 인터뷰(/welcome): 한 화면에 질문 하나, 탭으로 고르고, 뒤로 갈 수 있다. 마지막 화면에서 프로필을 저장하면 앱이 바로 바뀐다.

진행 중인 답은 서명된 세션 쿠키에 담아 두고(작은 글자뿐), 마지막 단계에서 한꺼번에 검사해 저장한다.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from pa.core import profile
from pa.web.auth import require_owner
from pa.web.ui import make_templates

router = APIRouter()
templates = make_templates()

# (키, 화면 제목, 도움말)
STEPS = [
    ("honorific", "어떻게 불러 드릴까요?", "예: 하늘님, 지은 씨. 비워 두면 '사용자님'이라고 불러요."),
    ("tone", "어떤 말투가 편하세요?", "비서가 대답할 때 쓰는 말투예요."),
    ("family", "함께 사는 가족을 알려 주세요", "식단 양과 영양을 맞추는 데 써요. 이름은 비워 두어도 돼요."),
    ("food", "못 먹거나 싫어하는 음식이 있나요?", "쉼표로 나눠 적어 주세요. 없으면 비워 두세요."),
    ("cook", "요리는 얼마나 하세요?", "식단의 어려움과 시간을 맞추는 데 써요."),
    ("budget", "일주일 식비는 얼마쯤 쓰세요?", "장보기 목록을 이 안에서 맞춰 볼게요."),
    ("shops", "주로 어디서 장을 보세요?", "여러 곳을 골라도 돼요."),
    ("interests", "요즘 관심 있는 것을 골라 주세요", "고른 것의 카드를 홈 화면 위쪽에 보여 드려요. 나중에 설정에서 바꿀 수 있어요."),
    ("look", "화면을 꾸며 볼까요?", "마음에 드는 모양과 글씨 크기를 골라 주세요."),
]
STEP_KEYS = [k for k, _, _ in STEPS]
BUDGET_PRESETS = (100000, 150000, 200000, 300000, 400000)
MEMBER_ROWS = profile.MAX_MEMBERS
_SESSION_KEY = "interview"


def _step(key: str) -> tuple[int, str, str]:
    if key not in STEP_KEYS:
        raise HTTPException(404, "없는 화면이에요")
    i = STEP_KEYS.index(key)
    return i, STEPS[i][1], STEPS[i][2]


def _data_dir(request: Request):
    return request.app.state.settings.data_dir


def _draft(request: Request) -> dict:
    """진행 중인 답. 없으면 저장된 프로필(다시 하기)이나 빈 값으로 시작한다."""
    d = request.session.get(_SESSION_KEY)
    if not isinstance(d, dict):
        d = dict(profile.load(_data_dir(request)) or {})
        request.session[_SESSION_KEY] = d
    return d


def _render(request: Request, key: str, error: str | None = None, status: int = 200, rows=None):
    i, title, help_ = _step(key)
    d = _draft(request)
    members = list(rows if rows is not None else d.get("members") or [])
    ctx = {"step": key, "n": i + 1, "total": len(STEPS), "title": title, "help": help_, "d": d, "error": error,
           "prev": f"/welcome/{STEP_KEYS[i - 1]}" if i else None, "last": i == len(STEPS) - 1,
           "members": members + [{}] * (MEMBER_ROWS - len(members)),
           "member_count": max(sum(1 for m in members if m.get("role") or m.get("age")), 1),
           "p": profile, "presets": BUDGET_PRESETS}
    return templates.TemplateResponse(request, "welcome.html", ctx, status_code=status)


@router.get("/welcome")
async def welcome_start(request: Request, restart: int = 0, _: str = Depends(require_owner)):
    if restart or _SESSION_KEY not in request.session:
        request.session.pop(_SESSION_KEY, None)
        _draft(request)
    return RedirectResponse(f"/welcome/{STEP_KEYS[0]}", status_code=303)


@router.get("/welcome/done")
async def welcome_done(request: Request, _: str = Depends(require_owner)):
    return templates.TemplateResponse(request, "welcome_done.html", {})


@router.get("/welcome/{key}")
async def welcome_step(key: str, request: Request, _: str = Depends(require_owner)):
    return _render(request, key)


def _read_answers(key: str, form) -> dict:
    """이 화면에서 받은 값만 골라 낸다."""
    if key == "honorific":
        return {"honorific": form.get("honorific", "")}
    if key == "tone":
        return {"tone": form.get("tone", "")}
    if key == "family":
        rows = [{"role": form.get(f"role_{i}", ""), "name": form.get(f"name_{i}", ""),
                 "age": form.get(f"age_{i}", ""), "sex": form.get(f"sex_{i}", "")} for i in range(MEMBER_ROWS)]
        return {"members": rows}
    if key == "food":
        return {"allergies": form.get("allergies", ""), "dislikes": form.get("dislikes", "")}
    if key == "cook":
        return {"cook_skill": form.get("cook_skill", ""), "weekday_cook_min": form.get("weekday_cook_min", "")}
    if key == "budget":
        custom = str(form.get("budget_custom", "")).strip()
        return {"weekly_budget_won": custom or form.get("budget_preset", "")}
    if key == "shops":
        return {"shops": form.getlist("shops")}
    if key == "interests":
        return {"interests": form.getlist("interests")}
    return {"theme": form.get("theme", ""), "font_size": form.get("font_size", "")}


def apply_profile(request: Request, prof: dict) -> None:
    """저장한 프로필을 실행 중인 앱에 바로 반영한다(말투·부르는 이름)."""
    apply_profile_to(request.app.state.router, prof)


def apply_profile_to(router_, prof: dict) -> None:
    for attr, val in (("tone", prof["tone"]), ("honorific", prof["honorific"])):
        try:
            setattr(router_, attr, val)
        except AttributeError:   # 테스트용 가짜 라우터 등
            pass


@router.post("/welcome/{key}")
async def welcome_save(key: str, request: Request, _: str = Depends(require_owner)):
    i, _title, _help = _step(key)
    form = await request.form()
    answers = _read_answers(key, form)
    d = _draft(request)
    try:
        norm = profile.normalize({**profile.defaults(), **d, **answers})   # 이 화면의 값이 틀리면 여기서 걸린다
    except profile.ProfileError as e:
        rows = answers.get("members")
        return _render(request, key, error=str(e), status=400, rows=rows)
    if i == len(STEPS) - 1:
        prof = profile.save(_data_dir(request), norm)
        request.session.pop(_SESSION_KEY, None)
        apply_profile(request, prof)
        return RedirectResponse("/welcome/done", status_code=303)
    request.session[_SESSION_KEY] = {**d, **{k: norm[k] for k in answers}}   # 이 화면에서 답한 것만 쌓는다
    return RedirectResponse(f"/welcome/{STEP_KEYS[i + 1]}", status_code=303)

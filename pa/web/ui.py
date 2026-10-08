"""웹 화면 공통: 템플릿 환경 + 하단 탭바용 컨텍스트(approvals_count). 동작 변경 없음(표시 전용)."""
from pathlib import Path

from fastapi.templating import Jinja2Templates

from pa.voice import prefs as voice_prefs
from pa.voice import voice_copy

TEMPLATE_DIR = Path(__file__).parent / "templates"


def base_context(request) -> dict:
    """승인 탭 배지용 대기 수. DB가 없거나 실패해도 화면은 그려진다."""
    try:
        n = request.app.state.con.execute("SELECT COUNT(*) FROM draft WHERE status='review'").fetchone()[0]
    except Exception:  # noqa: BLE001 - 배지는 표시 전용이라 어떤 실패에도 화면을 막지 않는다
        n = 0
    return {"approvals_count": n}


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR), context_processors=[base_context])
    templates.env.globals["voice_ui"] = voice_copy.UI           # 홈 음성 버튼의 화면 문구(라우트 컨텍스트는 그대로)
    templates.env.globals["voice_rates"] = voice_prefs.RATES
    return templates

from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from pa.core import notify, recs
from pa.scout import store as scout_store
from pa.scout.config import effective_config, load_scout_config
from pa.settings import Settings
from pa.voice.cache import AudioCache
from pa.voice.tts import NullTts
from pa.web import auth


def create_app(settings: Settings, con, router, *, http=None, tts=None) -> FastAPI:
    app = FastAPI(title="가족 비서", **({} if settings.dev_skip_tailscale
                                      else {"docs_url": None, "redoc_url": None, "openapi_url": None}))
    app.state.settings, app.state.con, app.state.router = settings, con, router
    recs.ensure_schema(con)
    scout_store.ensure_schema(con)
    notify.ensure_schema(con)
    app.state.scout_base = load_scout_config(settings.scout_config_path)
    app.state.scout_cfg = effective_config(con, app.state.scout_base)
    app.state.http = http if http is not None else httpx.AsyncClient(follow_redirects=True, timeout=10)
    app.state.tts = tts if tts is not None else NullTts()           # voice: default engine = none (V3 wires the real one)
    app.state.voice_cache = AudioCache(settings.data_dir / "voice_cache")
    # add_middleware 는 나중에 추가한 것이 바깥쪽. 요청 순서: ClientGuard > SecurityHeaders > HostAllowlist > CsrfGuard
    # > TailscaleGuard > Session > 라우트. 모두 pure ASGI(http+websocket).
    app.add_middleware(auth.TailscaleGuard, settings=settings)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, max_age=7 * 24 * 3600,
                       https_only=not settings.dev_skip_tailscale, same_site="lax", session_cookie="pa_session")
    app.add_middleware(auth.CsrfGuard, settings=settings)   # Host 허용 검사 직후, 세션/라우트 이전(이후 추가된 라우터에도 적용)
    app.add_middleware(auth.HostAllowlist, settings=settings)
    app.add_middleware(auth.SecurityHeaders)
    app.add_middleware(auth.ClientGuard)

    @app.get("/health")
    async def health():
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(auth.router)
    from pa.web import (routes, routes_google, routes_health, routes_meals, routes_routine, routes_scout,
                        routes_settings, routes_tasks, routes_voice)
    for mod in (routes, routes_scout, routes_google, routes_routine, routes_voice, routes_meals, routes_settings,
                routes_tasks, routes_health):
        app.include_router(mod.router)
    return app

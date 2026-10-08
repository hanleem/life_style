import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import uvicorn

from pa.backup.job import spawn_backup_tick
from pa.backup.runner import unsafe_location_warning
from pa.core import holidays, notify, worker
from pa.core.briefing import run_due_jobs
from pa.core.calendar_actions import write_allowlist
from pa.core.routine_jobs import run_routine_jobs
from pa.db import connect, init_db
from pa.integrations.gcal import GoogleCalendar
from pa.integrations.google_auth import SCOPES, GoogleAuth
from pa.llm.budget import Budget
from pa.llm.codex_cli import CodexCLI
from pa.llm.router import TaskRouter
from pa.logsafe import log_failure
from pa.rules import load_rules
from pa.scheduler import SCOUT_JOBS, due_jobs, mark_run
from pa.scout import store as scout_store
from pa.scout.config import effective_config, load_scout_config
from pa.scout.jobs import ScoutCtx, run_job
from pa.settings import Settings
from pa.web import auth as web_auth
from pa.web.app import create_app

log = logging.getLogger("pa")
KST = ZoneInfo("Asia/Seoul")
BIND_HOST = "127.0.0.1"   # 루프백 외 주소로 바꾸지 않는다


def check_exposure(s: Settings, host: str) -> None:
    """개발 모드(Tailscale 가드 해제)에서 루프백 외 바인딩이면 시작을 거부한다."""
    if s.dev_skip_tailscale and host != "127.0.0.1":
        raise SystemExit("dev_skip_tailscale 가 켜진 채로 127.0.0.1 이외 주소에 바인딩할 수 없습니다")


# 접근 로그에 남기지 않을 경로(경로 이름만으로 사용 내용이 드러나는 곳). 지금은 비어 있다.
PRIVATE_PATH_PREFIXES = web_auth.PRIVATE_PREFIXES   # 유일한 원천


def _is_private_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") or path.startswith(p + "?") for p in PRIVATE_PATH_PREFIXES)


class PrivatePathFilter(logging.Filter):
    """uvicorn.access 기록 중 가릴 경로의 줄을 버린다(uvicorn 은 args 에 (주소, 메서드, 경로, 버전, 상태)를 싣는다)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        if any(isinstance(a, str) and a.startswith("/") and _is_private_path(a) for a in args):
            return False
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(f'"{m} {p}' in msg for m in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
                       for p in PRIVATE_PATH_PREFIXES)


def install_access_log_filter() -> None:
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, PrivatePathFilter) for f in lg.filters):
        lg.addFilter(PrivatePathFilter())


async def briefing_tick(con, router, rules, cal, now) -> None:
    try:
        await run_due_jobs(con, now, rules, cal, router)
    except Exception as e:
        log_failure(log, "briefing tick failed", e)


async def routine_tick(con, rules, now) -> None:
    try:
        await run_routine_jobs(con, now, rules)
    except Exception as e:
        log_failure(log, "routine tick failed", e)


async def staples_tick(con, now) -> int:
    """개수를 세는 재료(즉석밥 등): 지난 끼니만큼 개수를 줄이고, 기준 이하로 남았으면 주문이 필요하다고 알린다(3일에 한 번).
    알림에는 재료 이름·남은 개수와 지난번에 산 상품 링크만 담는다. 실패해도 루프를 막지 않는다."""
    try:
        from pa.core import meals, pantry
        local = now.astimezone(meals.KST)
        pantry.consume_staples(con, meals.latest(con), local.date(), meals.load_config())
        sent = 0
        for s in pantry.low_staples(con):
            last = s.get("last_alert")
            if last and (now - datetime.fromisoformat(last)).days < 3:
                continue
            link = f" 지난번 상품: {s['url']}" if s.get("url") else ""
            text = f"{s['name']}이(가) {s['count']}{s['unit'] or '개'} 남았어요(기준 {s['reorder_at']}{s['unit'] or '개'} 이하). 주문이 필요해요.{link}"
            if await notify.announce(con, text, f"staple-{s['name']}-{local.date().isoformat()}"):
                con.execute("UPDATE staple SET last_alert=? WHERE name=?", (now.isoformat(), s["name"]))
                con.commit()
                sent += 1
        return sent
    except Exception as e:
        log.warning("staples tick failed (%s)", type(e).__name__)
        return 0


def pick_calendars(auth, con=None):
    """(읽기용, worker 쓰기용) 캘린더. 미연결이면 둘 다 None, 쓰기 scope 없으면 쓰기용만 None."""
    if not auth.connected():
        return None, None
    wids = (lambda: write_allowlist(con)) if con is not None else None
    cal = GoogleCalendar(auth.access_token, write_ids=wids)
    write = cal if SCOPES["calwrite"][0] in auth.granted() else None
    return cal, write


async def holiday_tick(con, auth, tick: int) -> None:
    """휴일 캘린더: 시작 직후 저장본을 올리고, 구글이 연결돼 있으면 매시간 갱신한다."""
    try:
        if tick == 0:
            holidays.load(con)
        if tick % 720 == 0 and auth.connected():
            cal = GoogleCalendar(auth.access_token, read_ids=[holidays.HOLIDAY_CAL_ID])
            now = datetime.now(timezone.utc)
            await holidays.refresh(con, cal, now.astimezone(KST).date(), now)
    except Exception as e:
        log_failure(log, "holiday tick failed", e)


async def scout_tick(ctx: ScoutCtx, base_cfg, now) -> None:
    try:
        ctx.cfg = effective_config(ctx.con, base_cfg)
        for job in due_jobs(ctx.con, now, ctx.rules, scout=ctx.cfg):
            if job not in SCOUT_JOBS or not mark_run(ctx.con, job, now):
                continue
            try:
                await run_job(job, ctx, now)
            except Exception as e:
                log_failure(log, f"scout job failed: {job}", e)
    except Exception as e:
        log_failure(log, "scout tick failed", e)


@dataclass
class LoopDeps:
    con: object
    router: object
    rules: object
    auth: object
    budget: object
    settings: object
    scout_ctx: object
    scout_base: object
    scout_state: dict = field(default_factory=dict)
    backup_state: dict = field(default_factory=dict)


def spawn_scout_tick(state: dict, ctx, base_cfg, now) -> "asyncio.Task | None":
    """scout_tick 을 백그라운드 단일 실행(single-flight)으로 띄운다. 이전 실행이 안 끝났으면 건너뛴다.
    AI 웹 검색이 오래 걸려도 다른 tick 이 막히지 않는다. 예외는 scout_tick 안에서 격리된다."""
    task = state.get("task")
    if task is not None and not task.done():
        return None
    state["task"] = asyncio.create_task(scout_tick(ctx, base_cfg, now))
    return state["task"]


async def loop_iteration(d: LoopDeps, tick: int) -> None:
    con, router, rules = d.con, d.router, d.rules
    try:
        cal, wcal = pick_calendars(d.auth, con)
    except Exception as e:
        log_failure(log, "google calendar setup failed", e)
        cal = wcal = None
    try:
        await worker.run_once(con, calendar=wcal)
        if tick % 120 == 0:          # 약 10분마다
            await worker.reconcile_unknown(con, calendar=wcal)
    except Exception as e:
        log_failure(log, "worker tick failed", e)
    await holiday_tick(con, d.auth, tick)
    await routine_tick(con, rules, datetime.now(timezone.utc))
    try:   # 백업은 스레드에서 도는 single-flight 잡. 어떤 실패도 루프를 막지 않는다.
        spawn_backup_tick(d.backup_state, con, d.settings, rules, datetime.now(timezone.utc))
    except Exception as e:
        log.error("backup spawn failed (%s)", type(e).__name__)
    await briefing_tick(con, router, rules, cal, datetime.now(timezone.utc))
    spawn_scout_tick(d.scout_state, d.scout_ctx, d.scout_base, datetime.now(timezone.utc))
    if tick % 720 == 0:   # 약 1시간마다
        await staples_tick(con, datetime.now(timezone.utc))


def build():
    if os.environ.pop("OPENAI_API_KEY", None):
        log.warning("OPENAI_API_KEY 환경변수를 제거했습니다 — ChatGPT 로그인으로만 처리합니다")
    install_access_log_filter()
    s = Settings()
    if not s.dev_skip_tailscale and (s.session_secret == "dev-only-change-me" or len(s.session_secret) < 32):
        raise SystemExit("PA_SESSION_SECRET 이 설정되지 않았거나 너무 짧습니다(32자 이상 필요). "
                         ".env 에 무작위 비밀값을 설정한 뒤 다시 시작하세요.")
    unsafe_location_warning(s)     # 백업 위치가 안전하지 않으면 경고(서버는 시작, 백업만 막힌다)
    check_exposure(s, BIND_HOST)   # main() 경로 한정. 실제 방어는 auth.ClientGuard(접속 상대 주소 검사)
    if s.dev_skip_tailscale:
        log.warning("DEV MODE: Tailscale guard disabled")
    elif not s.allowed_tailscale_login:
        log.info("PA_ALLOWED_TAILSCALE_LOGIN 이 비어 있습니다: 이 PC(루프백)에서만 접속할 수 있습니다")
    con = connect(s.core_db_path)
    init_db(con)
    budget = Budget(con, s.budget_ai_per_5h, s.budget_reserve_for_user,
                    per_day_web=s.budget_ai_web_per_day)
    router = TaskRouter(con, CodexCLI(s.codex_bin), budget,
                        Path(__file__).resolve().parent.parent / "config" / "prompts")
    rules = load_rules()
    auth = GoogleAuth(s, con)   # 연결/scope 상태는 tick마다 확인(재시작 없이 반영)
    app = create_app(s, con, router)
    scout_store.ensure_schema(con)
    scout_base = load_scout_config(s.scout_config_path)
    scout_ctx = ScoutCtx(con=con, router=router, rules=rules, cfg=scout_base,
                         http=httpx.AsyncClient(follow_redirects=True, timeout=15, trust_env=False))
    deps = LoopDeps(con=con, router=router, rules=rules, auth=auth, budget=budget, settings=s,
                    scout_ctx=scout_ctx, scout_base=scout_base)

    async def loop():
        worker.recover_on_startup(con)
        tick = 0
        while True:
            await loop_iteration(deps, tick)
            tick += 1
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(loop())
        yield
        task.cancel()
        st = deps.scout_state.get("task")
        if st is not None:
            st.cancel()
        bt = deps.backup_state.get("task")
        if bt is not None and not bt.done():
            # 스레드는 취소할 수 없고 rename 직전에 끊기면 partial 만 남는다 — 최대 30초 기다린다.
            try:
                await asyncio.wait_for(asyncio.shield(bt), 30)
            except asyncio.TimeoutError:
                log.warning("backup still running at shutdown")
            except Exception:
                pass

    app.router.lifespan_context = lifespan
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    install_access_log_filter()
    # tailscale serve 는 루프백에서 연결하며 X-Forwarded-For 를 붙인다. 프록시 헤더 재작성은 전혀 쓰지 않는다
    # (scope client 를 100.x 로 바꾸면 소유자가 잠기거나, 반대로 위조 헤더에 의존하게 된다).
    uvicorn.run(build(), host=BIND_HOST, port=8010, proxy_headers=False, forwarded_allow_ips="")


if __name__ == "__main__":
    main()

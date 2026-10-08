"""스카우트(나들이) 통합 검증: DB 호환, 격리, 알림, 라우트 인증, 피드백 저장."""
import ast
import asyncio
import pathlib
import re
import sqlite3
from datetime import datetime, timezone

import pytest
from conftest import owner_client
from fastapi.testclient import TestClient

from pa.core import coach_copy, notify, recs
from pa.db import SCHEMA_VERSION, connect, init_db
from pa.llm.router import TASK_TIER
from pa.scout import store
from pa.settings import Settings
from pa.web.app import create_app

ROOT = pathlib.Path(__file__).resolve().parent.parent / "pa"
NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def _tables(con):
    return {r[0]: [tuple(c)[1:5] for c in con.execute(f"PRAGMA table_info({r[0]})")]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _imports(path):
    mods = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            mods.add(n.module or "")
            mods |= {f"{n.module}.{a.name}" for a in n.names}
    return mods


# ---- DB ----------------------------------------------------------------

def test_scout_tables_do_not_collide_and_schema_is_idempotent(tmp_path):
    fresh = connect(tmp_path / "a.db")
    init_db(fresh)
    base = set(_tables(fresh))
    recs.ensure_schema(fresh)
    store.ensure_schema(fresh)
    t1 = _tables(fresh)
    recs.ensure_schema(fresh)
    store.ensure_schema(fresh)
    init_db(fresh)
    assert _tables(fresh) == t1
    assert {"outing_suggestion", "recommendation", "feedback", "taste_weight"} <= set(t1)
    # 스카우트 쪽 CREATE 와 schema.sql 이 같은 이름을 쓰는 테이블은 preference 하나뿐이고 정의가 같아야 한다
    scout_defs = {m.group(1): m.group(0) for src in (recs._SCHEMA, store._SCHEMA)
                  for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \(.*?\n?\);", src, re.S)}
    shared = set(scout_defs) & set(base)
    assert shared == {"preference"}
    ref = sqlite3.connect(":memory:")
    ref.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    alt = sqlite3.connect(":memory:")
    alt.execute(scout_defs["preference"])
    assert [tuple(c) for c in ref.execute("PRAGMA table_info(preference)")] == \
        [tuple(c) for c in alt.execute("PRAGMA table_info(preference)")]
    assert [tuple(c) for c in ref.execute("PRAGMA table_info(preference)")]   # 비어 있지 않음
    assert fresh.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


# ---- router / notify coexistence ----------------------------------------

def test_task_tier_and_notify_kinds():
    assert {"scout_search", "meal_plan", "summarize", "secretary_chat"} <= set(TASK_TIER)
    assert {"routine_supply", "approval_request", "briefing_morning", "meal_plan"} <= set(notify.ALLOWED)
    assert set(notify.ALLOWED) == set(notify._TITLES)
    assert all(cfg["tier"] in ("ai", "ai_web") for cfg in TASK_TIER.values())   # 로컬 모델 단계는 없다


def test_web_tools_only_for_scout():
    for name, cfg in TASK_TIER.items():
        if "WebFetch" in cfg.get("allowed_tools", "") or "WebSearch" in cfg.get("allowed_tools", ""):
            assert name.startswith("scout_") and cfg["tier"] == "ai_web", name


def test_all_notify_titles_neutral_and_safe():
    for kind in notify.ALLOWED:
        assert coach_copy.notify_safe(notify.render(kind, 3)), kind


async def test_notify_sends_only_fixed_kinds(con):
    assert await notify.send_notify(con, "meal_plan", 2, "k1")
    assert not await notify.send_notify(con, "meal_plan", 2, "k1")   # 같은 키는 한 번만
    with pytest.raises(ValueError):
        await notify.send_notify(con, "scout_free_text", 1, "k2")


# ---- isolation matrix ----------------------------------------------------

def test_scout_modules_never_import_app_internals():
    for f in (ROOT / "scout").rglob("*.py"):
        for m in _imports(f):
            assert not m.startswith(("pa.agents", "pa.web", "pa.core.worker", "pa.core.approval",
                                     "pa.core.routine", "pa.core.supply", "pa.core.briefing")), (f.name, m)


def test_supply_and_routine_modules_never_import_scout():
    for rel in ("core/supply.py", "core/routine_plan.py", "core/routine_jobs.py", "core/coach_copy.py",
                "web/routes_routine.py"):
        assert not any(m.startswith("pa.scout") for m in _imports(ROOT / rel)), rel


def test_outbound_http_helpers_only_used_by_scout_modules():
    for f in ROOT.rglob("*.py"):
        rel = f.relative_to(ROOT).as_posix()
        if rel.startswith("scout/"):
            continue
        assert not any(m.startswith("pa.scout.urlcheck") for m in _imports(f)), rel


def test_ssrf_guard_and_body_cap_still_wired():
    from pa.scout import urlcheck
    src = (ROOT / "scout/urlcheck.py").read_text(encoding="utf-8")
    assert "follow_redirects=False" in src and urlcheck.MAX_BYTES <= 65536 and urlcheck.MAX_REDIRECTS <= 5
    for h in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "100.64.0.1", "localhost", "pc." + "ts" + ".net", "[::1]",
              "::ffff:127.0.0.1"):
        assert asyncio.run(urlcheck.host_is_public(h)) is False, h


# ---- web: auth + feedback storage ---------------------------------------

@pytest.fixture
def web(con, tmp_path):
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True,
                 scout_config_path=tmp_path / "none.yaml")
    app = create_app(s, con, object())
    return app, con


def test_every_scout_route_requires_owner(web):
    from pa.web import routes_scout
    from pa.web.auth import require_owner
    app, _ = web
    c = TestClient(app)   # 이 PC 가 아닌 접속(소유자 아님)
    seen = 0
    for r in routes_scout.router.routes:
        assert require_owner in [d.call for d in r.dependant.dependencies], r.path
        for m in r.methods - {"HEAD", "OPTIONS"}:
            seen += 1
            url = re.sub(r"\{[^}]+\}", "x", r.path)
            resp = c.request(m, url, json={} if m != "GET" else None)
            assert resp.status_code in (401, 403), (m, r.path, resp.status_code)
    assert seen >= 8


def test_feedback_stores_only_enum_rating_and_chips(web):
    app, con = web
    c = owner_client(app)
    con.execute("INSERT INTO outing_suggestion(id,for_week,course_json,sources_json,status,created_at) "
                "VALUES('s1','w','{}','[]','proposed','x')")
    rid = recs.record_recommendation(con, domain="outing", item_ref="s1", features={"oneway_bucket": "30-60"},
                                     query_used="q", now=NOW)
    con.commit()
    free = c.post("/api/feedback", json={"rec_id": rid, "rating": "dislike", "reasons": ["자유 입력 사유"]})
    assert free.status_code == 200   # 칩 목록 밖 사유는 걸러지고 저장되지 않는다
    assert "자유 입력 사유" not in con.execute("SELECT reasons_json FROM feedback").fetchone()[0]
    assert c.post("/api/feedback", json={"rec_id": rid, "rating": "엄청 좋음"}).status_code == 422
    con.execute("DELETE FROM feedback")
    ok = c.post("/api/feedback", json={"rec_id": rid, "rating": "like", "reasons": [], "note": "좋아요"})
    assert ok.status_code == 200
    row = con.execute("SELECT rating, reasons_json, note FROM feedback").fetchone()
    assert row["rating"] == "like" and row["note"] == "좋아요"
    # 메모는 어떤 scout 입력으로도 읽히지 않는다
    for f in (ROOT / "scout").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "f.note" not in text and "\"note\"" not in text


# ---- main loop -----------------------------------------------------------

async def test_scout_tick_isolates_exceptions(con, tmp_path):
    from pa import main

    class Ctx:
        pass

    ctx = Ctx()
    ctx.con = None   # effective_config 가 예외를 던진다
    await main.scout_tick(ctx, None, datetime.now(timezone.utc))   # 예외 전파 없음


def test_main_loop_ticks_all_isolated():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    for fn in ("routine_tick", "briefing_tick", "scout_tick"):
        body = src.split(f"async def {fn}")[1].split("\nasync def ")[0].split("\ndef ")[0]
        assert "except Exception" in body, fn

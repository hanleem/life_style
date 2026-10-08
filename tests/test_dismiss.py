from conftest import owner_client
"""오늘 할 일 상자를 옆으로 밀어 숨기기(읽음 처리)."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from pa.core import dismiss

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=KST)


def _item(i, source="todo"):
    return {"id": f"todo:{i}", "text": f"할 일 {i}", "source": source, "reasons": ["이번 주 마감"], "date": "2026-10-08"}


def _briefing(con, must=3, rest=2):
    items = [_item(i) for i in range(1, must + rest + 1)]
    sec = {"today_must": items[:must], "rest_count": rest, "rest_items": [{k: t[k] for k in ("text", "source", "reasons", "date")} | {"id": t["id"]} for t in items[must:]]}
    con.execute("INSERT INTO briefing(id,kind,for_date,body_md,sections_json,created_at) VALUES('b','morning','2026-10-07','',?,'t')",
                (json.dumps(sec, ensure_ascii=False),))
    return items


def _sec(con):
    return json.loads(con.execute("SELECT sections_json FROM briefing").fetchone()[0])


def test_hide_removes_item_and_promotes_one_from_rest(con):
    _briefing(con)
    got = dismiss.hide(con, "todo:2", NOW)
    assert got["text"] == "할 일 2"
    s = _sec(con)
    assert [t["id"] for t in s["today_must"]] == ["todo:1", "todo:3", "todo:4"]       # 4번이 올라온다
    assert [t["id"] for t in s["rest_items"]] == ["todo:5"] and s["rest_count"] == 1
    assert dismiss.keys(con) == {"todo:2"}


def test_hide_from_rest_only_shrinks_the_rest(con):
    _briefing(con)
    dismiss.hide(con, "todo:5", NOW)
    s = _sec(con)
    assert [t["id"] for t in s["today_must"]] == ["todo:1", "todo:2", "todo:3"]
    assert [t["id"] for t in s["rest_items"]] == ["todo:4"] and s["rest_count"] == 1


def test_hide_is_idempotent_and_survives_unknown_keys(con):
    _briefing(con)
    dismiss.hide(con, "todo:1", NOW)
    before = _sec(con)
    dismiss.hide(con, "todo:1", NOW)                       # 두 번 눌러도 안전
    assert _sec(con) == before
    assert dismiss.hide(con, "todo:999", NOW) is None      # 없는 키도 숨김 목록에는 남아 다음 브리핑에서 빠진다
    assert "todo:999" in dismiss.keys(con)


def test_undo_puts_it_back(con):
    _briefing(con)
    dismiss.hide(con, "todo:2", NOW)
    assert dismiss.undo(con, "todo:2", NOW) is True
    s = _sec(con)
    ids = [t["id"] for t in s["today_must"]] + [t["id"] for t in s["rest_items"]]
    assert "todo:2" in ids and dismiss.keys(con) == set()
    assert dismiss.undo(con, "todo:2", NOW) is False        # 이미 되돌렸다


def test_future_briefings_skip_dismissed_items(con):
    import asyncio
    from pa.core.briefing import build_briefing
    from pa.integrations.calendar import FakeCalendar
    from pa.rules import load_rules
    for i in (1, 2, 3):
        con.execute("INSERT INTO task(id,title,bucket,due_at,status,created_at,updated_at) "
                    "VALUES(?,?,'check_soon','2026-10-07T03:00:00+00:00','open','2026-10-05T01:00:00+00:00','2026-10-05T01:00:00+00:00')",
                    (f"t{i}", f"할일{i}"))
    dismiss.hide(con, "task:t2", NOW)
    class R:
        async def run(self, *a, **k):
            return None
    b = asyncio.run(build_briefing(con, "morning", NOW, FakeCalendar(), R(), load_rules()))
    texts = [t["text"] for t in b["sections"]["today_must"]]
    assert "할일1" in texts and "할일3" in texts and "할일2" not in texts


def _client(con, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from conftest import freeze_clock
    from pa.settings import Settings
    from pa.web.app import create_app
    from test_web_voice import T_MORNING
    freeze_clock(monkeypatch, T_MORNING)
    s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
    c = owner_client(create_app(s, con, None))
    return c


def _today_briefing(con, must=3, rest=2):
    items = [_item(i) for i in range(1, must + rest + 1)]
    sec = {"today_must": items[:must], "rest_count": rest,
           "rest_items": [{k: t[k] for k in ("text", "source", "reasons", "date")} | {"id": t["id"]} for t in items[must:]]}
    con.execute("INSERT INTO briefing(id,kind,for_date,body_md,sections_json,created_at) VALUES('b','morning','2026-10-06','',?,'t')",
                (json.dumps(sec, ensure_ascii=False),))


def test_api_requires_owner_and_validates_key(con, tmp_path, monkeypatch):
    c = _client(con, tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    stranger = TestClient(c.app, base_url="http://localhost:8010", client=("203.0.113.9", 5000))   # 이 PC가 아닌 기기
    assert stranger.post("/api/dismiss", json={"key": "todo:1"}).status_code in (401, 403)
    assert c.post("/api/dismiss", json={"key": "../etc"}).status_code == 422
    assert c.post("/api/dismiss", json={"key": "todo:1; drop table task"}).status_code == 422


def test_swipe_api_hides_and_undo_restores_on_the_pages(con, tmp_path, monkeypatch):
    _today_briefing(con)
    c = _client(con, tmp_path, monkeypatch)
    home = c.get("/").text
    assert 'data-swipe-key="todo:2"' in home and 'id="swipe-js"' in home
    r = c.post("/api/dismiss", json={"key": "todo:2"})
    assert r.status_code == 200 and r.json() == {"ok": True, "label": "할 일 2"}
    home, today = c.get("/").text, c.get("/today").text
    assert "할 일 2" not in home and 'data-swipe-key="todo:4"' in home                # 4번이 올라왔다
    assert "읽음 처리한 항목" in today and 'data-undo-key="todo:2"' in today
    assert c.post("/api/dismiss/undo", json={"key": "todo:2"}).json() == {"ok": True}
    assert "할 일 2" in c.get("/today").text and "읽음 처리한 항목" not in c.get("/today").text

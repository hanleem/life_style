"""Stage 7 V2: voice script API, home listen button, settings page. Offline (FakeTts/NullTts)."""
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import freeze_clock, owner_client
from fastapi.testclient import TestClient

from pa.settings import Settings
from pa.voice import prefs, script, source, voice_copy
from pa.voice.tts import FakeTts
from pa.web import routes_voice
from pa.web.app import create_app
from pa.web.ui import TEMPLATE_DIR, make_templates

ROOT = Path(__file__).resolve().parents[1]
PARTIAL = TEMPLATE_DIR / "_voice_home.html"
# 2026-10-06 (Tue) 08:40 KST
T_MORNING = datetime(2026, 10, 5, 23, 40, tzinfo=timezone.utc)
# 2026-10-06 21:00 KST (after the Tue evening briefing time 20:30)
T_EVENING = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
TODAY = "2026-10-06"


def _sec(**kw):
    base = {"today_must": [], "rest_count": 0, "calendar": [], "approvals": 0}
    return {**base, **kw}


def _briefing(con, sections, kind="morning", for_date=TODAY, body_md=""):
    con.execute("INSERT INTO briefing(id,kind,for_date,body_md,sections_json,created_at) VALUES(?,?,?,?,?,'t')",
                (f"b-{kind}-{for_date}", kind, for_date, body_md, json.dumps(sections, ensure_ascii=False)))


@pytest.fixture
def mk(con, tmp_path, monkeypatch):
    freeze_clock(monkeypatch, T_MORNING)   # 홈(routes_routine._now)·음성·pa.clock 모두 같은 시각

    def make(tts=None, login=True):
        s = Settings(session_secret="t" * 32, dev_skip_tailscale=True, data_dir=tmp_path)
        c = (owner_client if login else TestClient)(create_app(s, con, None, **({"tts": tts} if tts is not None else {})))
        return c
    return make


@pytest.fixture
def client(mk):
    return mk()


def test_voice_routes_require_login(mk):
    c = mk(login=False)
    assert c.get("/api/voice/briefing").status_code == 401
    assert c.get("/settings/voice").status_code == 401
    assert c.put("/api/voice/settings", json={"rate": 1.2}).status_code == 401


def test_script_api_no_briefing(client):
    r = client.get("/api/voice/briefing")
    assert r.status_code == 200 and r.json() == {"available": False, "reason": "no_briefing"}
    assert r.headers["cache-control"] == "no-store"


def test_script_api_returns_sentences_and_hash(client, con):
    _briefing(con, _sec(today_must=[{"text": "A"}], rest_count=1, approvals=2))
    r = client.get("/api/voice/briefing")
    j = r.json()
    assert r.headers["cache-control"] == "no-store" and r.headers["pragma"] == "no-cache"
    assert j["available"] is True and j["briefing_kind"] == "morning" and j["for_date"] == TODAY
    assert j["sentences"][0] == "시월 육일 화요일, 아침 브리핑입니다."
    assert len(j["script_hash"]) == 16 and j["engine_ready"] is False and j["rate"] == 1.0
    assert isinstance(j["est_seconds"], int)


def test_script_api_engine_ready_with_fake_tts(mk, con):
    _briefing(con, _sec())
    assert mk(FakeTts()).get("/api/voice/briefing").json()["engine_ready"] is True


def test_script_api_disabled(client, con):
    _briefing(con, _sec())
    prefs.set_settings(con, enabled=False)
    r = client.get("/api/voice/briefing")
    assert r.json() == {"available": False, "reason": "disabled"}


def test_titles_are_not_spoken_by_default(client, con):
    _briefing(con, _sec(today_must=[{"text": "고유제목ZZ"}], calendar=[{"title": "일정제목QQ", "time": "15:00"}]))
    assert "고유제목ZZ" not in client.get("/api/voice/briefing").text
    assert "일정제목QQ" not in client.get("/api/voice/briefing").text


def test_rate_reflects_settings(client, con):
    _briefing(con, _sec())
    prefs.set_settings(con, rate=1.5)
    assert client.get("/api/voice/briefing").json()["rate"] == 1.5


def test_evening_after_evening_time(mk, con, monkeypatch):
    _briefing(con, _sec())
    _briefing(con, {"done_today": 2, "left_today": 1, "retro": None, "approvals": 0}, kind="evening")
    c = mk()
    assert c.get("/api/voice/briefing").json()["briefing_kind"] == "morning"
    monkeypatch.setattr(routes_voice, "_now", lambda: T_EVENING)
    assert c.get("/api/voice/briefing").json()["briefing_kind"] == "evening"


def test_other_days_briefing_not_used(client, con):
    _briefing(con, _sec(), for_date="2026-10-05")
    assert client.get("/api/voice/briefing").json()["reason"] == "no_briefing"


def test_corrupt_sections_json_is_unavailable_not_500(client, con):
    con.execute("INSERT INTO briefing(id,kind,for_date,body_md,sections_json,created_at) VALUES('x','morning',?,'','{not json','t')", (TODAY,))
    r = client.get("/api/voice/briefing")
    assert r.status_code == 200 and r.json()["available"] is False


def test_body_md_is_never_read_or_spoken(client, con):
    _briefing(con, _sec(), body_md="> 비밀요약문장XYZ 오늘 수면 부족")
    assert "비밀요약문장XYZ" not in client.get("/api/voice/briefing").text


def test_unknown_future_keys_are_ignored(client, con):
    """Trunk may soon add mail.candidate_count and mail_tasks: the allowlist must ignore them."""
    sec = _sec(mail={"reply_count": 1, "titles": ["메일제목KK"], "error": None, "candidate_count": 7,
                     "tasks": [{"text": "메일할일PP"}]},
               mail_tasks=[{"text": "메일할일PP", "from": "a@example.com"}], future_key={"text": "미래값FF"})
    _briefing(con, sec)
    base = client.get("/api/voice/briefing").json()
    _briefing_no_extra = script.build_script(
        "morning", datetime(2026, 10, 6).date(),
        {**sec, "mail": {"reply_count": 1, "titles": ["메일제목KK"], "error": None}},
        opts=script.ScriptOptions()).sentences
    assert tuple(base["sentences"]) == _briefing_no_extra
    blob = json.dumps(base, ensure_ascii=False)
    for leak in ("메일제목KK", "메일할일PP", "미래값FF", "a@example.com", "candidate"):
        assert leak not in blob


def test_script_builder_ignores_unknown_keys_directly():
    d = datetime(2026, 10, 6).date()
    a = script.build_script("morning", d, _sec(mail={"reply_count": 1, "titles": [], "error": None}), opts=script.ScriptOptions())
    b = script.build_script("morning", d, _sec(mail={"reply_count": 1, "titles": [], "error": None, "candidate_count": 4},
                                                mail_tasks=[{"text": "x"}], zzz=1), opts=script.ScriptOptions())
    assert a.script_hash == b.script_hash


def test_friday_tail_comes_from_friday_briefing(client, con, monkeypatch):
    fri_t = datetime(2026, 10, 8, 23, 40, tzinfo=timezone.utc)      # Fri 2026-10-09 08:40 KST
    monkeypatch.setattr(routes_voice, "_now", lambda: fri_t)
    _briefing(con, _sec(), for_date="2026-10-09")
    base = client.get("/api/voice/briefing").json()
    _briefing(con, {"done_week": 4, "calendar": [{"title": "t", "time": "10:00"}], "lab": {}}, kind="friday", for_date="2026-10-09")
    with_fri = client.get("/api/voice/briefing").json()
    assert with_fri["briefing_kind"] == "morning"
    assert len(with_fri["sentences"]) > len(base["sentences"])


def test_source_reads_only_allowed_tables(client, con):
    _briefing(con, _sec())
    sqls: list[str] = []
    con.set_trace_callback(sqls.append)
    try:
        assert client.get("/api/voice/briefing").status_code == 200
        assert client.put("/api/voice/settings", json={"rate": 1.2}).status_code == 200
    finally:
        con.set_trace_callback(None)
    tables = set()
    for q in sqls:
        tables |= {t.lower() for t in re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)", q, flags=re.I)} - {"set"}
    assert {"briefing", "preference"} <= tables
    assert tables <= {"briefing", "preference"}, tables
    # the briefing read never selects body_md
    assert not any(re.search(r"body_md|SELECT\s+\*", q, re.I) for q in sqls if "briefing" in q.lower())


def test_voice_api_does_not_write_or_call_llm(mk, con):
    _briefing(con, _sec())

    class Boom:
        async def run(self, *a, **k):
            raise AssertionError("LLM must not be called")

    c = mk()
    c.app.state.router = Boom()
    before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("briefing", "job_run", "llm_call", "held_notify")}
    assert c.get("/api/voice/briefing").status_code == 200
    assert c.get("/settings/voice").status_code == 200
    after = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after


def test_settings_put_validates(client, con):
    r = client.put("/api/voice/settings", json={"rate": 2.0})
    assert r.status_code == 400 and r.headers["cache-control"] == "no-store"
    r = client.put("/api/voice/settings", json={"rate": 1.2})
    assert r.status_code == 200 and r.json()["rate"] == 1.2 and r.headers["cache-control"] == "no-store"
    assert prefs.get_settings(con).rate == 1.2
    assert client.put("/api/voice/settings", json={"engine": "edge"}).status_code == 400
    assert client.put("/api/voice/settings", json={"bogus": True}).status_code == 400
    assert client.put("/api/voice/settings", json={"say_todo_titles": "yes"}).status_code == 400
    assert client.put("/api/voice/settings", json=[1]).status_code == 400
    assert client.put("/api/voice/settings", content=b"{nope", headers={"content-type": "application/json"}).status_code == 400
    assert client.put("/api/voice/settings", content=b'{"rate":1.5}', headers={"content-type": "text/plain"}).status_code == 415
    assert prefs.get_settings(con).rate == 1.2


def test_settings_put_is_atomic_across_keys(client, con):
    r = client.put("/api/voice/settings", json={"say_todo_titles": True, "rate": 9})
    assert r.status_code == 400 and prefs.get_settings(con).say_todo_titles is False


def test_settings_changes_script_options(client, con):
    _briefing(con, _sec(today_must=[{"text": "고유제목ZZ"}]))
    assert client.put("/api/voice/settings", json={"say_todo_titles": True}).status_code == 200
    assert "고유제목ZZ" in client.get("/api/voice/briefing").text


def test_settings_page_content(client):
    r = client.get("/settings/voice")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    html = r.text
    assert "이 PC에서만 소리를 만듭니다" in html
    assert voice_copy.SENSITIVE_WARNING in html
    for name in ("enabled", "engine", "rate", "say_todo_titles", "say_event_titles", "say_supply"):
        assert f'name="{name}"' in html
    assert 'style="' not in html and "autoplay" not in html
    assert 'aria-current="page"' not in html      # tab 없음


def test_settings_page_reflects_saved_values(client, con):
    prefs.set_settings(con, say_event_titles=True, rate=0.8)
    html = client.get("/settings/voice").text
    assert re.search(r'name="say_event_titles"[^>]*checked', html)
    assert not re.search(r'name="say_todo_titles"[^>]*checked', html)
    assert re.search(r'<option value="0.8"[^>]*selected', html)


def test_no_audio_route_in_this_task(client):
    """V3 owns /api/voice/briefing/audio; V2 must not define it (UI relies on 404/503 -> fallback)."""
    assert all(not getattr(r, "path", "").endswith("/audio") for r in routes_voice.router.routes)


def test_home_renders_voice_block_and_existing_content_unchanged(client, con):
    _briefing(con, _sec(today_must=[{"text": "A"}, {"text": "B"}, {"text": "C"}], rest_count=4))
    r = client.get("/")
    html = r.text
    assert r.status_code == 200
    assert 'id="vb-play"' in html and "🔊 오늘 브리핑 듣기 (1분)" in html
    assert html.index('id="vb"') > html.index("<h2>오늘</h2>")
    assert all(x in html for x in ("A", "B", "C")) and "그 외 4건" in html


def test_voice_partial_has_no_autoplay_and_no_inline_style():
    src = PARTIAL.read_text(encoding="utf-8")
    assert "autoplay" not in src.lower() and 'style="' not in src and "style='" not in src
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", src)
    assert "innerHTML" not in src and "outerHTML" not in src and "insertAdjacentHTML" not in src
    assert not re.search(r"<audio[^>]*\ssrc=", src), "audio url must never be a static attribute"
    assert "hidden" in src


def _func_bodies(src: str, name: str) -> list[tuple[int, int]]:
    spans = []
    for m in re.finditer(rf"(?:async\s+)?function\s+{name}\s*\(", src):
        i = src.index("{", m.end())
        depth, j = 0, i
        while True:
            depth += {"{": 1, "}": -1}.get(src[j], 0)
            if depth == 0:
                break
            j += 1
        spans.append((i, j))
    return spans


def test_play_calls_only_inside_startplayback_and_again():
    src = PARTIAL.read_text(encoding="utf-8")
    allowed = _func_bodies(src, "startPlayback") + _func_bodies(src, "again")
    assert len(allowed) == 2
    calls = [m.start() for m in re.finditer(r"\.play\(", src)]
    assert calls
    for pos in calls:
        assert any(a < pos < b for a, b in allowed), src[max(0, pos - 40):pos + 20]


def test_no_load_time_playback():
    src = PARTIAL.read_text(encoding="utf-8")
    for m in re.finditer(r"(DOMContentLoaded|onload|setTimeout|setInterval|'load')", src):
        window = src[m.start(): m.start() + 300]
        assert "startPlayback" not in window and "again(" not in window and ".play(" not in window, window
    # startPlayback is reachable only from the click handler
    callers = [m.start() for m in re.finditer(r"startPlayback\s*\(", src)]
    defs = [a for a, _ in _func_bodies(src, "startPlayback")]
    assert len(callers) == len(defs) + 1


def test_local_voice_filter_requires_localService():
    src = PARTIAL.read_text(encoding="utf-8")
    assert re.search(r"localService\s*===\s*true", src)
    assert not re.search(r"localService\s*(==|!=|!==)\s*false", src)
    spans = _func_bodies(src, "speakLocal")
    assert len(spans) == 1
    a, b = spans[0]
    speaks = [m.start() for m in re.finditer(r"speechSynthesis\.speak\(", src)]
    assert speaks and all(a < p < b for p in speaks)
    body = src[a:b]
    assert body.index("localService") < body.index("speechSynthesis.speak(")


def test_ui_strings_come_from_voice_copy():
    html = make_templates().env.get_template("_voice_home.html").render()
    ui = voice_copy.UI
    assert re.search(r'id="vb-play"[^>]*>\s*' + re.escape(ui["play"]) + r"\s*<", html)
    assert re.search(r'id="vb-stop"[^>]*>\s*' + re.escape(ui["stop"]) + r"\s*<", html)
    assert ui["text"] in html and ui["rate"] in html
    for r in prefs.RATES:
        assert f'<option value="{r}"' in html
    m = re.search(r"const UI\s*=\s*(\{.*?\});", html, re.S)
    assert json.loads(m.group(1)) == ui


# ---------------------------------------------------------------------------------------------
# Behaviour of the page script under node with stubs: fallback on 404/503, no autoplay, 409 retry.
# ---------------------------------------------------------------------------------------------

HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scenario = JSON.parse(process.argv[3]);
const js = /<script>([\s\S]*?)<\/script>/.exec(html)[1];
function el(id){ return {id, hidden:false, disabled:false, textContent:'', dataset:{}, children:[], value:'1',
  open:false, listeners:{}, attrs:{},
  addEventListener(t,f){ (this.listeners[t]=this.listeners[t]||[]).push(f); },
  appendChild(c){ this.children.push(c); }, replaceChildren(){ this.children=[]; },
  setAttribute(k,v){ this.attrs[k]=v; }, removeAttribute(k){ delete this.attrs[k]; }, focus(){} }; }
const els = {};
for (const id of ['vb','vb-play','vb-stop','vb-rate','vb-status','vb-caption','vb-text','vb-lines','vb-audio']) els[id]=el(id);
const audio = els['vb-audio']; audio.playCalls = 0; audio.paused = true; audio.playbackRate = 1;
audio.play = function(){ this.playCalls++; if(scenario.playReject && this.src && !String(this.src).startsWith('data:')) return Promise.reject(Object.assign(new Error('x'),{name:'NotAllowedError'})); this.paused=false; return Promise.resolve(); };
audio.pause = function(){ this.paused = true; };
const fetches = []; const speaks = []; const cancels = []; const posts = [];
if (scenario.bridge === 'ok') global.PABridge = { postMessage: m => { posts.push(m); } };
if (scenario.bridge === 'nofn') global.PABridge = { postMessage: 'x' };
const docListeners = {};
global.document = { getElementById: id => els[id], createElement: t => el(t), hidden:false,
  addEventListener(t,f){ (docListeners[t]=docListeners[t]||[]).push(f); } };
const winListeners = {};
global.window = global; global.addEventListener = (t,f)=>{ (winListeners[t]=winListeners[t]||[]).push(f); };
global.URL = { createObjectURL: () => 'blob:fake', revokeObjectURL(){} };
global.Blob = class {};
if (scenario.speech) {
  global.speechSynthesis = { getVoices: () => scenario.voices, speak: u => { speaks.push(u); }, cancel: () => cancels.push(1), pause(){}, resume(){}, addEventListener(t, f){ setTimeout(f, 0); } };
  global.SpeechSynthesisUtterance = class { constructor(t){ this.text = t; } };
}
let infoCalls = 0;
global.fetch = async (url, opt) => {
  fetches.push([String(url), (opt && opt.method) || 'GET']);
  if (String(url).startsWith('/api/voice/briefing/audio')) {
    const st = scenario.audioStatuses.shift();
    return { ok: st === 200, status: st, blob: async () => new global.Blob() };
  }
  if (String(url) === '/api/voice/briefing') {
    infoCalls++;
    return { ok: true, status: 200, json: async () => scenario.info };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};
global.setTimeout = global.setTimeout;
eval(js);
(async () => {
  const wait = () => new Promise(r => setTimeout(r, 20));
  await wait();
  const before = { playCalls: audio.playCalls, speaks: speaks.length, audioFetch: fetches.filter(f=>f[0].startsWith('/api/voice/briefing/audio')).length };
  if (scenario.tap !== false) { for (const f of els['vb-play'].listeners.click||[]) f({}); await wait(); await wait(); }
  let stopped = null;
  if (scenario.stop) { for (const f of els['vb-stop'].listeners.click||[]) f({}); await wait();
    stopped = { state: els['vb'].dataset.state, paused: audio.paused, cancels: cancels.length, stopHidden: els['vb-stop'].hidden }; }
  console.log(JSON.stringify({ before, state: els['vb'].dataset.state, status: els['vb-status'].textContent,
    playLabel: els['vb-play'].textContent, playDisabled: els['vb-play'].disabled, playCalls: audio.playCalls,
    audioSrc: audio.src || null, speaks: speaks.map(s => [s.text, s.voice && s.voice.name, s.lang]),
    audioFetches: fetches.filter(f=>f[0].startsWith('/api/voice/briefing/audio')).length, infoCalls,
    textOpen: els['vb-text'].open, textHidden: els['vb-text'].hidden, lines: els['vb-lines'].children.map(c=>c.textContent),
    posts, stopped, puts: fetches.filter(f=>f[1]==='PUT').length }));
})();
"""

INFO = {"available": True, "reason": None, "briefing_kind": "morning", "for_date": TODAY, "sentences": ["하나입니다.", "둘입니다."],
        "est_seconds": 5, "script_hash": "abcd1234abcd1234", "rate": 1.0, "engine_ready": True}
KO_LOCAL = {"name": "ko-local", "lang": "ko-KR", "localService": True}
KO_REMOTE = {"name": "ko-remote", "lang": "ko-KR", "localService": False}
HAVE_NODE = shutil.which("node") is not None


def _run(tmp_path, **scenario):
    h = tmp_path / "h.js"
    h.write_text(HARNESS, encoding="utf-8")
    page = tmp_path / "p.html"
    page.write_text(make_templates().env.get_template("_voice_home.html").render(), encoding="utf-8")
    out = subprocess.run(["node", str(h), str(page), json.dumps(scenario)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


pytestmark_node = pytest.mark.skipif(not HAVE_NODE, reason="node not installed")


@pytestmark_node
def test_js_no_playback_on_load(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[200], tap=False, speech=True, voices=[KO_LOCAL])
    assert r["before"]["playCalls"] == 0 and r["before"]["speaks"] == 0 and r["before"]["audioFetch"] == 0
    assert r["state"] == "idle"


@pytestmark_node
def test_js_tap_plays_audio_blob(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[200])
    assert r["state"] == "playing" and r["audioSrc"] == "blob:fake" and r["audioFetches"] == 1
    assert r["lines"] == INFO["sentences"]


@pytestmark_node
@pytest.mark.parametrize("status", [404, 503, 500])
def test_js_audio_404_503_falls_back_to_local_voice(tmp_path, status):
    r = _run(tmp_path, info=INFO, audioStatuses=[status], speech=True, voices=[KO_REMOTE, KO_LOCAL])
    assert r["audioFetches"] == 1
    assert [s[0] for s in r["speaks"]] == INFO["sentences"]
    assert all(s[1] == "ko-local" and s[2] == "ko-KR" for s in r["speaks"])


@pytestmark_node
def test_js_remote_only_voices_are_never_used(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[404], speech=True, voices=[KO_REMOTE, {"name": "en", "lang": "en-US", "localService": True}])
    assert r["speaks"] == [] and r["state"] == "error"
    assert r["textHidden"] is False and r["textOpen"] is True and r["lines"] == INFO["sentences"]


@pytestmark_node
def test_js_no_speech_support_shows_text_error(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[503], speech=False)
    assert r["state"] == "error" and r["status"] == voice_copy.UI["error"] and r["textOpen"] is True


@pytestmark_node
def test_js_engine_not_ready_skips_audio_fetch(tmp_path):
    r = _run(tmp_path, info={**INFO, "engine_ready": False}, audioStatuses=[], speech=True, voices=[KO_LOCAL])
    assert r["audioFetches"] == 0 and len(r["speaks"]) == 2


@pytestmark_node
def test_js_409_retries_info_once(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[409, 200])
    assert r["audioFetches"] == 2 and r["state"] == "playing"
    assert r["infoCalls"] >= 2


@pytestmark_node
def test_js_play_rejected_goes_to_ready_with_again_label(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[200], playReject=True)
    assert r["state"] == "ready" and r["playLabel"] == voice_copy.UI["again"]


@pytestmark_node
@pytest.mark.parametrize("info,key", [({"available": False, "reason": "no_briefing"}, "no_briefing"),
                                      ({"available": False, "reason": "disabled"}, "disabled")])
def test_js_unavailable_states(tmp_path, info, key):
    r = _run(tmp_path, info=info, audioStatuses=[], tap=False)
    assert r["state"] == "unavailable" and r["playDisabled"] is True and r["status"] == voice_copy.UI[key]


@pytestmark_node
def test_js_stop_button_resets(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[200], stop=True)
    assert r["state"] == "idle" or r["stopped"]["state"] == "idle"
    assert r["stopped"]["state"] == "idle" and r["stopped"]["paused"] is True and r["stopped"]["stopHidden"] is True


@pytestmark_node
def test_js_stop_cancels_local_speech(tmp_path):
    r = _run(tmp_path, info={**INFO, "engine_ready": False}, audioStatuses=[], speech=True, voices=[KO_LOCAL], stop=True)
    assert r["stopped"]["state"] == "idle" and r["stopped"]["cancels"] >= 1


# ---- Stage 7 (plan A): phone-native TTS through the phone WebView bridge is primary ----

def _long_info(n_sentences, each=60):
    sents = [("가" * (each - 8) + f" 문장{i:03d}입니다.") for i in range(n_sentences)]
    return {**INFO, "sentences": sents}


@pytestmark_node
def test_js_bridge_tap_posts_speak_and_skips_audio_and_speech(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[200], bridge="ok", speech=True, voices=[KO_LOCAL])
    assert [json.loads(p) for p in r["posts"]] == [{"op": "speak", "text": " ".join(INFO["sentences"])}]
    assert r["audioFetches"] == 0 and r["speaks"] == [] and r["state"] == "playing"
    assert r["lines"] == INFO["sentences"]


@pytestmark_node
def test_js_bridge_never_posts_on_load(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[], bridge="ok", tap=False)
    assert r["posts"] == [] and r["state"] == "idle"


@pytestmark_node
def test_js_bridge_stop_posts_stop_speak(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[], bridge="ok", stop=True)
    ops = [json.loads(p)["op"] for p in r["posts"]]
    assert ops == ["speak", "stop_speak"] and r["stopped"]["state"] == "idle"


@pytestmark_node
def test_js_bridge_truncates_at_sentence_boundary(tmp_path):
    info = _long_info(30)
    r = _run(tmp_path, info=info, audioStatuses=[], bridge="ok")
    text = json.loads(r["posts"][0])["text"]
    assert 0 < len(text) <= 700
    assert text.endswith("입니다.")
    joined = " ".join(info["sentences"])
    assert joined.startswith(text) and joined[len(text)] == " "
    assert text.split(" ") == [w for w in joined.split(" ")][: len(text.split(" "))]


@pytestmark_node
def test_js_bridge_short_script_is_not_truncated(tmp_path):
    r = _run(tmp_path, info=INFO, audioStatuses=[], bridge="ok")
    assert json.loads(r["posts"][0])["text"] == "하나입니다. 둘입니다."


@pytestmark_node
def test_js_bridge_single_oversized_sentence_cut_at_word_boundary(tmp_path):
    words = ["단어%03d" % i for i in range(300)]
    info = {**INFO, "sentences": [" ".join(words) + "."]}
    r = _run(tmp_path, info=info, audioStatuses=[], bridge="ok")
    text = json.loads(r["posts"][0])["text"]
    assert 0 < len(text) <= 700
    assert all(w in words for w in text.split(" "))


@pytestmark_node
def test_js_bridge_unavailable_briefing_posts_nothing(tmp_path):
    r = _run(tmp_path, info={"available": False, "reason": "no_briefing"}, audioStatuses=[], bridge="ok")
    assert r["posts"] == []


@pytestmark_node
def test_js_without_bridge_old_path_runs(tmp_path):
    r = _run(tmp_path, info={**INFO, "engine_ready": False}, audioStatuses=[], speech=True, voices=[KO_LOCAL])
    assert r["posts"] == [] and [s[0] for s in r["speaks"]] == INFO["sentences"]


@pytestmark_node
def test_js_bridge_without_postmessage_function_uses_old_path(tmp_path):
    r = _run(tmp_path, info={**INFO, "engine_ready": False}, audioStatuses=[], bridge="nofn", speech=True, voices=[KO_LOCAL])
    assert r["posts"] == [] and len(r["speaks"]) == 2


def test_bridge_code_does_not_log_text():
    src = PARTIAL.read_text(encoding="utf-8")
    assert "console." not in src


def test_script_api_kind_morning_replay_after_evening(con, mk, monkeypatch):
    """저녁 브리핑이 생긴 뒤에도 ?kind=morning 으로 아침 브리핑을 다시 들을 수 있다."""
    _briefing(con, _sec(today_must=[{"text": "아침 일", "source": "app"}]), kind="morning")
    _briefing(con, _sec(), kind="evening")
    freeze_clock(monkeypatch, T_EVENING)
    c = mk()
    assert c.get("/api/voice/briefing").json()["briefing_kind"] == "evening"
    r = c.get("/api/voice/briefing?kind=morning").json()
    assert r["available"] and r["briefing_kind"] == "morning"


def test_script_api_kind_missing_or_invalid(con, mk):
    _briefing(con, _sec(), kind="evening")
    c = mk()
    r = c.get("/api/voice/briefing?kind=morning").json()
    assert r == {"available": False, "reason": "no_briefing"}      # 다른 브리핑으로 바꿔 읽지 않는다
    assert c.get("/api/voice/briefing?kind=bogus").status_code == 422


def test_home_has_morning_replay_button(con, mk):
    html = mk().get("/").text
    assert 'id="vb-morning"' in html and voice_copy.UI["morning_again"] in html

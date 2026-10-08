"""홈·오늘 자세히의 '옆으로 밀어 읽음 처리' 스크립트 동작을 node 로 흉내 내서 확인한다(손가락 이벤트)."""
import json
import re
import shutil
import subprocess

import pytest

from pa.web.ui import TEMPLATE_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

HARNESS = r"""
const src = process.argv[1];
const scenario = JSON.parse(process.argv[2]);
const calls = [];
const listeners = {};
function mkEl(extra) {
  return Object.assign({
    dataset: {}, style: {}, hidden: false, textContent: '', offsetWidth: 100, removed: false,
    on: {}, addEventListener(t, f) { this.on[t] = f; }, querySelectorAll() { return []; }, remove() { this.removed = true; }
  }, extra || {});
}
const bar = mkEl(), txt = mkEl(), btn = mkEl();
const box = mkEl({dataset: {swipeKey: 'todo:2'}});
const readBtn = mkEl();
box.querySelectorAll = (sel) => (sel === '[data-read]' ? [readBtn] : []);
global.document = {
  getElementById: (id) => ({'undo-bar': bar, 'undo-text': txt, 'undo-btn': btn})[id],
  querySelectorAll: (sel) => (sel === '[data-swipe-key]' ? [box] : []),
};
global.location = {reload() { calls.push(['reload']); }};
global.toast = (t) => calls.push(['toast', t]);
global.fetch = async (url, opt) => { calls.push(['fetch', url, JSON.parse(opt.body)]); return {ok: scenario.fetchOk !== false, json: async () => ({ok: true, label: '할 일 2'})}; };
eval(src);
const t = (x, y) => ({touches: [{clientX: x, clientY: y}]});
(async () => {
  for (const step of scenario.steps) {
    if (step[0] === 'start') box.on.touchstart(t(step[1], step[2]));
    if (step[0] === 'move') box.on.touchmove(t(step[1], step[2]));
    if (step[0] === 'end') box.on.touchend({});
    if (step[0] === 'click_read') readBtn.on.click({preventDefault() {}});
    if (step[0] === 'undo') await btn.on.click();
    await new Promise((r) => setTimeout(r, 5));
  }
  await new Promise((r) => setTimeout(r, 260));
  console.log(JSON.stringify({calls, transform: box.style.transform, opacity: box.style.opacity, removed: box.removed,
                              barHidden: bar.hidden, text: txt.textContent}));
})();
"""


def _script() -> str:
    html = (TEMPLATE_DIR / "_swipe.html").read_text(encoding="utf-8")
    return re.search(r'<script id="swipe-js">(.*?)</script>', html, re.S).group(1)


def _run(steps, **kw):
    out = subprocess.run(["node", "-e", HARNESS, _script(), json.dumps({"steps": steps, **kw})],
                         capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_swiping_far_enough_hides_the_box_and_shows_undo():
    r = _run([["start", 100, 100], ["move", 60, 102], ["move", 30, 103], ["end"]])
    assert ["fetch", "/api/dismiss", {"key": "todo:2"}] in r["calls"]
    assert r["removed"] is True and r["barHidden"] is False and "읽음 처리했습니다: 할 일 2" in r["text"]


def test_swiping_the_other_direction_works_too():
    r = _run([["start", 20, 100], ["move", 70, 101], ["move", 95, 101], ["end"]])
    assert ["fetch", "/api/dismiss", {"key": "todo:2"}] in r["calls"]


def test_short_swipe_snaps_back_without_calling_the_server():
    r = _run([["start", 100, 100], ["move", 85, 101], ["move", 80, 101], ["end"]])
    assert not [c for c in r["calls"] if c[0] == "fetch"]
    assert r["transform"] == "" and r["removed"] is False


def test_vertical_scroll_is_ignored():
    r = _run([["start", 100, 100], ["move", 98, 160], ["move", 40, 220], ["end"]])
    assert not [c for c in r["calls"] if c[0] == "fetch"] and r["removed"] is False


def test_failed_request_restores_the_box_and_tells_the_user():
    r = _run([["start", 100, 100], ["move", 30, 101], ["end"]], fetchOk=False)
    assert r["removed"] is False and r["transform"] == ""
    assert ["toast", "처리하지 못했습니다. 다시 시도해 주세요"] in r["calls"]


def test_read_button_is_the_pc_fallback_and_undo_reloads():
    r = _run([["click_read"], ["undo"]])
    assert ["fetch", "/api/dismiss", {"key": "todo:2"}] in r["calls"]
    assert ["fetch", "/api/dismiss/undo", {"key": "todo:2"}] in r["calls"] and ["reload"] in r["calls"]

"""홈 화면에 추가(PWA): 매니페스트, 서비스 워커, 설치 안내, 알림 구독. 매니페스트·서비스 워커는 브라우저가 로그인 정보 없이 읽는다."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from pa.core import profile, push
from pa.web.auth import require_owner
from pa.web.ui import make_templates, theme_colors

router = APIRouter()
templates = make_templates()

ICONS = [
    {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
]

# 서비스 워커: 푸시를 알림으로 보여 주고, 눌렀을 때 같은 사이트 안의 화면만 연다. 화면·데이터는 저장하지 않는다(개인 정보).
SW_JS = """\
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener('push', function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  var title = typeof data.title === 'string' && data.title ? data.title : '가족 비서';
  var href = typeof data.href === 'string' && data.href.startsWith('/') && !data.href.startsWith('//') ? data.href : '/';
  event.waitUntil(self.registration.showNotification(title, {
    icon: '/static/icons/icon-192.png', badge: '/static/icons/icon-192.png', data: { href: href }
  }));
});
self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var href = (event.notification.data && event.notification.data.href) || '/';
  if (typeof href !== 'string' || !href.startsWith('/') || href.startsWith('//')) { href = '/'; }
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if ('focus' in list[i]) { list[i].navigate(href); return list[i].focus(); }
    }
    return self.clients.openWindow(href);
  }));
});
"""


@router.get("/manifest.webmanifest")
async def manifest(request: Request):
    prof = profile.load(request.app.state.settings.data_dir)
    theme = profile.theme_attrs(prof)["theme"]
    c = theme_colors(theme)
    body = {"name": "가족 비서", "short_name": "가족 비서", "description": "우리 가족의 식단·할 일·일정을 챙겨 주는 비서",
            "start_url": "/", "scope": "/", "display": "standalone", "lang": "ko", "orientation": "portrait",
            "theme_color": c["headerFrom"], "background_color": c["paper"], "icons": ICONS}
    return Response(json.dumps(body, ensure_ascii=False), media_type="application/manifest+json")


@router.get("/sw.js")
async def service_worker():
    return Response(SW_JS, media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@router.get("/install")
async def install_guide(request: Request, _: str = Depends(require_owner)):
    resp = templates.TemplateResponse(request, "install.html", {
        "vapid_public": push.ensure_keys(request.app.state.settings.data_dir)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _device_id(owner: str) -> str:
    return owner.removeprefix("device:") if owner.startswith("device:") else "pc"


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "요청이 올바르지 않아요") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "요청이 올바르지 않아요")
    return body


@router.post("/api/push/subscribe")
async def push_subscribe(request: Request, owner: str = Depends(require_owner)):
    body = await _json_body(request)
    try:
        push.subscribe(request.app.state.con, _device_id(owner), body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


@router.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, owner: str = Depends(require_owner)):
    body = await _json_body(request)
    endpoint = body.get("endpoint")
    ok = isinstance(endpoint, str) and push.unsubscribe(request.app.state.con, _device_id(owner), endpoint)
    return JSONResponse({"ok": bool(ok)})
